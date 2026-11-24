"""批量评测 runner（对应 PRD FR-705 / FR-706、M5 每周回归与发版前跑分）。

给新手的说明：
- "评测集"就是一批带标准答案的题目（JSONL，一行一题）。跑评测 = 把每道题都检索一遍，
  比对"应该命中的文档"有没有出现在前 K 条里，然后算三个经典指标：
  · Recall@K（本处用命中率口径）：前 K 条里出现正确文档的题目占比
  · MRR：正确文档第一次出现的位置的倒数（第 1 名=1，第 2 名=0.5……）
  · nDCG@K：考虑排序位置的折损，越靠前得分越高
- 合规评测不是检索：对每道题跑一遍 precheck，看"该拦的有没有拦、不该拦的有没有误拦"。
  **红线：expected=block 但实际没拦（redline_miss）必须为 0**。
- 每次跑完把结果存库（eval_run）+ 落 JSON 报告，才能看"这周比上周好了还是差了"。

重要诚实说明：
- 本仓库自带的评测集用于**链路验收与回归**，规模约 100 条、文档集较小，分数会偏高；
  生产验收必须用业务专家标注的 ≥200 条真实问题集（PRD 11.3–11.7），别拿开发集数字当结论。
"""
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import BASE_DIR, get_settings
from app.models.ops import EvalRun
from app.models.user import User
from app.services.compliance import precheck
from app.services.embedding import embedder_state
from app.services.reranker import reranker_state
from app.services.retrieval import retrieve


@dataclass
class EvalCase:
    id: str
    query: str
    expected_docs: list[str] = field(default_factory=list)
    expected_keywords: list[str] = field(default_factory=list)
    lang: str = "zh"
    category: str = "general"
    expected_action: str | None = None  # compliance 集：pass / annotate / block
    note: str = ""

    @property
    def is_compliance(self) -> bool:
        return self.expected_action is not None


def eval_root() -> Path:
    p = Path(get_settings().eval_dir)
    return p if p.is_absolute() else BASE_DIR / p


def _read_jsonl(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with path.open("r", encoding="utf-8-sig") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name} 第 {lineno} 行 JSON 解析失败：{exc}") from exc
            cases.append(EvalCase(
                id=str(raw.get("id") or f"{path.stem}-{lineno}"),
                query=raw["query"],
                expected_docs=list(raw.get("expected_docs") or []),
                expected_keywords=list(raw.get("expected_keywords") or []),
                lang=raw.get("lang", "zh"),
                category=raw.get("category", "general"),
                expected_action=raw.get("expected_action"),
                note=raw.get("note", ""),
            ))
    return cases


def list_datasets() -> list[dict]:
    """列出可用评测集（含题目数、语种分布、是否为合规集）。"""
    root = eval_root()
    out: list[dict] = []
    if not root.exists():
        return out
    for path in sorted(root.glob("*.jsonl")):
        try:
            cases = _read_jsonl(path)
        except ValueError as exc:
            out.append({"name": path.stem, "error": str(exc), "cases": 0})
            continue
        langs: dict[str, int] = {}
        for c in cases:
            langs[c.lang] = langs.get(c.lang, 0) + 1
        out.append({
            "name": path.stem,
            "cases": len(cases),
            "languages": langs,
            "kind": "compliance" if cases and all(c.is_compliance for c in cases) else "retrieval",
            "categories": sorted({c.category for c in cases}),
        })
    return out


def load_dataset(name: str) -> list[EvalCase]:
    path = eval_root() / f"{name}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"评测集不存在：{name}")
    return _read_jsonl(path)


def _is_relevant(hit: dict, case: EvalCase) -> bool:
    doc = (hit.get("doc_name") or "").lower()
    for expected in case.expected_docs:
        e = expected.lower()
        if e and (e in doc or doc in e):
            return True
    content = (hit.get("content") or "").lower()
    return any(k.lower() in content for k in case.expected_keywords if k)


def _rank_metrics(case: EvalCase, hits: list[dict], k: int) -> dict:
    flags = [1 if _is_relevant(h, case) else 0 for h in hits[:k]]
    first = next((i + 1 for i, flag in enumerate(flags) if flag), None)
    dcg = sum(flag / math.log2(i + 2) for i, flag in enumerate(flags))
    ideal_n = max(1, min(len(case.expected_docs) or 1, k))
    idcg = sum(1 / math.log2(i + 2) for i in range(ideal_n))
    return {
        "hit": 1 if first else 0,
        "first_rank": first,
        "mrr": round(1 / first, 6) if first else 0.0,
        "ndcg": round(dcg / idcg, 6) if idcg else 0.0,
    }


def _avg(rows: list[dict], key: str) -> float:
    return round(sum(r[key] for r in rows) / len(rows), 4) if rows else 0.0


def _retrieval_eval(
    db: Session, user: User, cases: list[EvalCase], *, top_k: int,
    crosslingual: bool, rerank: bool, limit: int | None, rerank_top_n: int,
) -> dict:
    selected = cases[:limit] if limit else cases
    per_case: list[dict] = []
    channels = ("vector", "bm25", "hybrid")
    channel_rows: dict[str, list[dict]] = {c: [] for c in channels}
    started = time.perf_counter()

    for case in selected:
        t0 = time.perf_counter()
        result = retrieve(
            db=db, user=user, query=case.query, top_k=top_k,
            crosslingual=crosslingual, rerank=rerank, rerank_top_n=rerank_top_n,
        )
        case_row = {
            "id": case.id, "query": case.query, "lang": case.lang, "category": case.category,
            "expected_docs": case.expected_docs, "expected_keywords": case.expected_keywords,
            "took_ms": int((time.perf_counter() - t0) * 1000),
            "channels": {},
            "top_docs": [h.get("doc_name") for h in result.get("hybrid", [])[:top_k]],
            "degraded": result.get("degraded", False),
        }
        for channel in channels:
            m = _rank_metrics(case, result.get(channel, []), top_k)
            channel_rows[channel].append(m)
            case_row["channels"][channel] = m
        per_case.append(case_row)

    summary = {
        channel: {
            "recall": _avg(rows, "hit"),
            "mrr": _avg(rows, "mrr"),
            "ndcg": _avg(rows, "ndcg"),
            "cases": len(rows),
        }
        for channel, rows in channel_rows.items()
    }
    # 分组（语种 / 类别）：只看 hybrid，便于发现"西语/阿语掉分"
    for dimension in ("lang", "category"):
        groups: dict[str, list[dict]] = {}
        for row in per_case:
            groups.setdefault(row[dimension], []).append(row["channels"]["hybrid"])
        summary[f"by_{dimension}"] = {
            key: {"recall": _avg(rows, "hit"), "mrr": _avg(rows, "mrr"),
                  "ndcg": _avg(rows, "ndcg"), "cases": len(rows)}
            for key, rows in sorted(groups.items())
        }
    return {
        "kind": "retrieval",
        "cases": len(selected),
        "top_k": top_k,
        "crosslingual": crosslingual,
        "rerank": rerank,
        "summary": summary,
        "details": per_case,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def _compliance_eval(
    db: Session, user: User, cases: list[EvalCase], *, limit: int | None,
) -> dict:
    selected = cases[:limit] if limit else cases
    started = time.perf_counter()
    details: list[dict] = []
    confusion: dict[str, int] = {}
    redline_missed: list[str] = []
    false_blocks: list[str] = []

    for idx, case in enumerate(selected):
        decision = precheck(db, user, case.query, request_id=f"eval{idx:04d}")
        actual = decision.action
        expected = case.expected_action or "pass"
        ok = actual == expected
        notice_ok = expected != "annotate" or bool(decision.notice)
        if expected == "block" and actual != "block":
            redline_missed.append(case.id)
        if expected != "block" and actual == "block":
            false_blocks.append(case.id)
        confusion[f"{expected}->{actual}"] = confusion.get(f"{expected}->{actual}", 0) + 1
        details.append({
            "id": case.id, "query": case.query, "lang": case.lang, "category": case.category,
            "expected": expected, "actual": actual, "ok": ok, "notice_ok": notice_ok,
            "layer": decision.layer, "hit_terms": [h.get("term") for h in decision.hits][:5],
        })

    n = len(selected) or 1
    blocked_expected = sum(1 for d in details if d["expected"] == "block")
    blocked_actual = sum(1 for d in details if d["actual"] == "block")
    true_block = sum(1 for d in details if d["expected"] == "block" and d["actual"] == "block")
    summary = {
        "accuracy": round(sum(1 for d in details if d["ok"]) / n, 4),
        "notice_accuracy": round(sum(1 for d in details if d["notice_ok"]) / n, 4),
        "block_recall": round(true_block / blocked_expected, 4) if blocked_expected else None,
        "block_precision": round(true_block / blocked_actual, 4) if blocked_actual else None,
        "redline_missed": redline_missed,
        "false_blocks": false_blocks,
        "confusion": confusion,
        "cases": len(selected),
    }
    return {
        "kind": "compliance",
        "cases": len(selected),
        "summary": summary,
        "details": details,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def run_eval(
    db: Session,
    user: User,
    dataset: str,
    *,
    top_k: int = 5,
    limit: int | None = None,
    crosslingual: bool | None = None,
    rerank: bool = True,
    rerank_top_n: int = 10,
    save: bool = True,
) -> dict:
    """跑一个评测集并归档。返回完整报告（含逐题明细）。"""
    cases = load_dataset(dataset)
    if not cases:
        raise ValueError(f"评测集为空：{dataset}")

    started = time.perf_counter()
    if all(c.is_compliance for c in cases):
        report = _compliance_eval(db, user, cases, limit=limit)
        recall = mrr = ndcg = 0.0
        accuracy = report["summary"]["accuracy"]
    else:
        # 默认：跨语言集开启变体检索，中文集关闭（避免无谓翻译调用）
        use_cross = crosslingual if crosslingual is not None else any(c.lang != "zh" or c.category == "crosslingual" for c in cases)
        report = _retrieval_eval(
            db, user, cases, top_k=top_k, crosslingual=use_cross,
            rerank=rerank, limit=limit, rerank_top_n=rerank_top_n,
        )
        recall = report["summary"]["hybrid"]["recall"]
        mrr = report["summary"]["hybrid"]["mrr"]
        ndcg = report["summary"]["hybrid"]["ndcg"]
        accuracy = None

    report.update({
        "dataset": dataset,
        "created_by": user.username,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "runtime": {
            "embedder": embedder_state(),
            "reranker": reranker_state(),
            "vector_store": get_settings().vector_store,
        },
        "duration_ms": int((time.perf_counter() - started) * 1000),
    })

    if save:
        record = EvalRun(
            dataset=dataset, top_k=top_k, cases=report["cases"],
            recall=recall, mrr=mrr, ndcg=ndcg, accuracy=accuracy,
            duration_ms=report["duration_ms"],
            detail_json=json.dumps(report, ensure_ascii=False),
            created_by=user.username,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        report["run_id"] = record.id
        # 报告落盘（带时间戳，便于对比历史；仅保留明细，体积可控）
        report_dir = Path(get_settings().eval_report_dir)
        if not report_dir.is_absolute():
            report_dir = BASE_DIR / report_dir
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = report_dir / f"{dataset}-{stamp}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(path)
    return report


def append_badcase_sample(
    case: EvalCase, *, dataset: str = "draft_badcases",
) -> Path:
    """badcase 一键转评测样本（FR-803d）。

    写入草稿集：期望答案需要人工补齐（通常由修复后的正确文档决定），
    因此草稿先记录问题与来源，人工补 expected_docs 后再并入正式评测集。
    """
    root = eval_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{dataset}.jsonl"
    row = {
        "id": case.id, "query": case.query, "expected_docs": case.expected_docs,
        "expected_keywords": case.expected_keywords, "lang": case.lang,
        "category": case.category, "note": case.note,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path
