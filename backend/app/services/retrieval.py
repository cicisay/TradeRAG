"""混合检索服务（对应 PRD FR-202 跨语言双路、FR-203 混合检索 RRF、FR-603 权限过滤）。

给新手的说明：
- 一次搜索 = 三路结果合并：
  ① 向量路：语义相近（bge-m3 天然跨语言）
  ② BM25 路：关键词精确（型号/单号/缩写）
  ③ 跨语言路：把问题翻成英/西/阿再各检索一遍（翻译不可用时自动降级并标记）
- 融合用 RRF（Reciprocal Rank Fusion）：只看"排名"不看原始分数。
  因为向量分（0~1）和 BM25 分（0~几十）量纲不同，直接相加没意义；
  RRF 让每路的第 1 名贡献 1/(k+1)、第 2 名 1/(k+2)……稳定且不需要调权重。
- 权限：所有召回必须先经 build_access_expr（向量路）或 SQL 过滤（BM25 路），
  业务组不匹配的内容在检索阶段就被挡掉，不存在"先召回再筛"的漏洞。
"""
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.chunk import CHUNK_STATUS_ACTIVE, Chunk
from app.models.document import STATUS_PARSED, DocVersion, Document, DocumentMeta
from app.models.user import User
from app.services.bm25 import BM25Index
from app.services.embedding import get_embedder
from app.services import metrics, tracing
from app.services.reranker import get_reranker
from app.services.translator import get_translator
from app.services.vector_store import build_access_expr, get_vector_store

logger = logging.getLogger(__name__)

RRF_K = 60          # RRF 平滑常数（业界常用 60）
CANDIDATE_MULTIPLIER = 3  # 各路多取一些候选，供融合后截断
VECTOR_WEIGHT = 1.0       # 语义路权重（主）
BM25_WEIGHT = 0.6         # 关键词路权重（辅，避免中文 BM25 挤掉跨语言语义命中）
VARIANT_WEIGHT = 0.9      # 翻译变体路权重（略低于原查询）


@dataclass
class Hit:
    chunk_id: int
    content: str
    score: float
    channel: str
    doc_id: int | None = None
    doc_name: str = ""
    doc_version_id: int | None = None
    kb_id: int | None = None
    biz_group: str = ""
    language: str = "zh"
    channels: list[str] = field(default_factory=list)
    page_no: int | None = None        # 来源页码（PDF 文档才有）
    rerank_score: float | None = None  # 重排得分（未重排为 None）


def _accessible_rows(db: Session, user: User, kb_ids: list[int] | None) -> list[tuple[Chunk, Document, DocumentMeta]]:
    """取当前用户可见的 chunk（SQL 层过滤：业务组 + 文档状态 + 知识库范围）。

    这就是 BM25 路的权限闸门；向量路的闸门是 build_access_expr。两路同源同规则。
    """
    q = (
        db.query(Chunk, Document, DocumentMeta)
        .join(DocVersion, DocVersion.id == Chunk.doc_version_id)
        .join(Document, Document.id == DocVersion.doc_id)
        .join(DocumentMeta, DocumentMeta.doc_id == Document.id)
        .filter(Chunk.status == CHUNK_STATUS_ACTIVE, Document.status == STATUS_PARSED)
    )
    if user.role.code != "admin":
        q = q.filter(DocumentMeta.biz_group == user.biz_group)
    if kb_ids:
        q = q.filter(Document.kb_id.in_(kb_ids))
    return q.limit(5000).all()  # 开发规模上限；生产由 Milvus/OpenSearch 承载


def _hydrate(db: Session, chunk_ids: list[int]) -> dict[int, tuple[Chunk, Document, DocumentMeta]]:
    if not chunk_ids:
        return {}
    rows = (
        db.query(Chunk, Document, DocumentMeta)
        .join(DocVersion, DocVersion.id == Chunk.doc_version_id)
        .join(Document, Document.id == DocVersion.doc_id)
        .join(DocumentMeta, DocumentMeta.doc_id == Document.id)
        .filter(Chunk.id.in_(chunk_ids))
        .all()
    )
    return {c.id: (c, d, m) for c, d, m in rows}


def _user_kb_ids(db: Session, user: User, kb_ids: list[int] | None) -> list[int]:
    """未指定知识库时，取用户可见的全部知识库 id（管理员不限）。"""
    if kb_ids:
        return kb_ids
    from app.models.knowledge_base import KnowledgeBase

    q = db.query(KnowledgeBase.id)
    if user.role.code != "admin":
        q = q.filter(KnowledgeBase.biz_group == user.biz_group)
    return [row[0] for row in q.all()]


def vector_channel(db: Session, user: User, query: str, kb_ids: list[int], top_k: int) -> list[Hit]:
    from app.services.vector_store import sanitize_collection_name

    settings = get_settings()
    embedder = get_embedder()
    store = get_vector_store()
    collection = sanitize_collection_name(settings.milvus_collection)
    store.ensure_collection(collection, embedder.dim)

    expr = build_access_expr(
        kb_ids=kb_ids,
        biz_groups=["*"] if user.role.code == "admin" else [user.biz_group],
        max_sensitivity=3 if user.role.code == "admin" else 0,
    )
    # 管理员 expr 里业务组用通配：内存实现按记录值比较，需单独处理
    query_vec = embedder.embed([query])[0]
    if user.role.code == "admin":
        raw = store.search(collection, query_vec, expr=None, top_k=top_k * CANDIDATE_MULTIPLIER)
        raw = [h for h in raw if h.get("kb_id") in kb_ids or not kb_ids]
    else:
        raw = store.search(collection, query_vec, expr=expr, top_k=top_k * CANDIDATE_MULTIPLIER)

    chunk_ids = [h["chunk_id"] for h in raw]
    hydrated = _hydrate(db, chunk_ids)
    hits: list[Hit] = []
    for h in raw:
        row = hydrated.get(h["chunk_id"])
        if row is None:  # 已删除/归档的向量（防御性过滤）
            continue
        c, d, m = row
        hits.append(Hit(
            chunk_id=c.id, content=c.content, score=float(h["score"]), channel="vector",
            doc_id=d.id, doc_name=d.doc_name, doc_version_id=c.doc_version_id,
            kb_id=d.kb_id, biz_group=m.biz_group, language=c.language, channels=["vector"],
            page_no=c.page_no,
        ))
    return hits[: top_k * CANDIDATE_MULTIPLIER]


def bm25_channel(db: Session, user: User, query: str, kb_ids: list[int], top_k: int) -> list[Hit]:
    rows = _accessible_rows(db, user, kb_ids)
    if not rows:
        return []
    index = BM25Index()
    index.build([(c.id, c.content) for c, _, _ in rows])
    scored = index.search(query, top_k=top_k * CANDIDATE_MULTIPLIER)
    row_map = {c.id: (c, d, m) for c, d, m in rows}
    hits: list[Hit] = []
    for chunk_id, score in scored:
        c, d, m = row_map[chunk_id]
        hits.append(Hit(
            chunk_id=c.id, content=c.content, score=float(score), channel="bm25",
            doc_id=d.id, doc_name=d.doc_name, doc_version_id=c.doc_version_id,
            kb_id=d.kb_id, biz_group=m.biz_group, language=c.language, channels=["bm25"],
            page_no=c.page_no,
        ))
    return hits


def rrf_fuse(hit_lists: list[list[Hit]], weights: list[float] | None = None) -> list[Hit]:
    """RRF 融合：按 rank 而非原始分。同 chunk 跨路命中时合并通道标签。

    weights 可选：给不同通道不同权重。跨语言场景里中文查询的 BM25 会大量命中中文文档，
    与向量路的语义命中"抢排名"；把 BM25 权重降到 0.6，让语义路主导、关键词路补充，
    是业界常见做法（关键词对型号/单号仍然有效，只是不该压过语义）。
    """
    merged: dict[int, Hit] = {}
    for idx, hits in enumerate(hit_lists):
        weight = weights[idx] if weights and idx < len(weights) else 1.0
        for rank, hit in enumerate(hits):
            contribution = weight / (RRF_K + rank + 1)
            if hit.chunk_id in merged:
                existing = merged[hit.chunk_id]
                existing.score += contribution
                existing.channels.extend(hit.channels)
            else:
                clone = Hit(**{**hit.__dict__})
                clone.score = contribution
                merged[hit.chunk_id] = clone
    return sorted(merged.values(), key=lambda h: h.score, reverse=True)


def _brief(hits: list[Hit], n: int = 5) -> list[dict]:
    """给 Langfuse 看的命中摘要（每个子 span 的 input/output 内容）。

    为什么要瘦身：一个 chunk 全文动辄上千字，5 条就是几 KB，trace 体积会失控、
    上报变慢。这里只留"能判断召回对不对"的最小信息：谁、多少分、哪篇文档、开头片段。
    """
    return [
        {
            "chunk_id": h.chunk_id,
            "score": round(h.score, 4),
            "rerank": h.rerank_score,
            "doc": (h.doc_name or "")[:40],
            "lang": h.language,
            "page": h.page_no,
            "snippet": " ".join((h.content or "").split())[:80],
        }
        for h in hits[:n]
    ]


def retrieve(
    db: Session,
    user: User,
    query: str,
    kb_ids: list[int] | None = None,
    top_k: int = 10,
    crosslingual: bool = True,
    rerank: bool = True,
    rerank_top_n: int | None = None,
    lf: "tracing._Node | tracing._Noop | None" = None,
) -> dict:
    """执行混合检索（可按通道返回对照结果，供检索调试台使用）。

    rerank=False 用于批量评测的"重排前基线"跑分（同一批数据对比重排收益）；
    rerank_top_n 可覆盖配置里的候选数（批量评测用更小的候选数控制总时长）。
    lf：Langfuse 追踪节点（11-1）。传入时各通道耗时作为子 span 上报；
    None/未启用时零开销 no-op。
    """
    started = time.perf_counter()
    lf = lf if lf is not None else tracing._Noop()
    effective_kb_ids = _user_kb_ids(db, user, kb_ids)
    if not effective_kb_ids:
        return {"vector": [], "bm25": [], "hybrid": [], "variants": {}, "degraded": False, "took_ms": 0}

    with lf.span_cm("vector-channel") as sp:
        _t0 = time.perf_counter()
        vec_hits = vector_channel(db, user, query, effective_kb_ids, top_k)
        metrics.observe_channel_duration("vector", time.perf_counter() - _t0)
        metrics.observe_hits("vector", len(vec_hits))
        sp.update(
            input={"query": query, "kb_ids": effective_kb_ids, "top_k": top_k},
            output={"count": len(vec_hits), "hits": _brief(vec_hits)},
        )
    with lf.span_cm("bm25-channel") as sp:
        _t0 = time.perf_counter()
        bm_hits = bm25_channel(db, user, query, effective_kb_ids, top_k)
        metrics.observe_channel_duration("bm25", time.perf_counter() - _t0)
        metrics.observe_hits("bm25", len(bm_hits))
        sp.update(
            input={"query": query, "kb_ids": effective_kb_ids, "top_k": top_k},
            output={"count": len(bm_hits), "hits": _brief(bm_hits)},
        )
    hybrid = rrf_fuse([vec_hits, bm_hits], [VECTOR_WEIGHT, BM25_WEIGHT])[:top_k]

    # 跨语言变体路（FR-202a）：翻译成功则各语种并行检索后再融合
    variants: dict[str, str] = {}
    degraded = False
    if crosslingual:
        with lf.span_cm("translate-variants") as sp:
            variants, degraded = get_translator().query_variants(query, db=db)
            sp.update(
                input={"query": query, "targets": ["en", "es", "ar"]},
                output={"variants": variants, "degraded": degraded,
                        "count": len(variants)},
            )
        if variants:
            with lf.span_cm("variant-channels") as sp:
                variant_lists = [vec_hits, bm_hits]
                weights = [VECTOR_WEIGHT, BM25_WEIGHT]
                per_lang: dict[str, dict] = {}
                variant_hits = 0
                for lang, translated in variants.items():
                    vh = vector_channel(db, user, translated, effective_kb_ids, top_k)
                    bh = bm25_channel(db, user, translated, effective_kb_ids, top_k)
                    variant_hits += len(vh) + len(bh)
                    per_lang[lang] = {"vector": len(vh), "bm25": len(bh),
                                      "top": _brief(vh, 1)}
                    variant_lists.append(vh)
                    variant_lists.append(bh)
                    weights.extend([VARIANT_WEIGHT, BM25_WEIGHT * 0.8])
                hybrid = rrf_fuse(variant_lists, weights)[:top_k]
                metrics.observe_hits("variant", variant_hits)
                sp.update(
                    input={"variants": variants},
                    output={"per_lang": per_lang, "fused": len(hybrid),
                            "top": _brief(hybrid, 3)},
                )

    # 重排（FR-205）：从融合结果里取若干候选，按"与问题的相关度"重排后截断
    rerank_info: dict = {"provider": None, "applied": False, "candidates": len(hybrid)}
    reranker = get_reranker() if rerank else None
    if reranker is not None and hybrid:
        with lf.span_cm("rerank") as sp:
            candidate_n = max(rerank_top_n or get_settings().rerank_top_n, top_k)
            candidates = hybrid[:candidate_n]
            ranked = reranker.rerank(query, candidates, top_k)
            # 前后对照最有价值：能看出重排把哪条从第 N 提到了第 1
            sp.update(
                input={"query": query, "candidates": len(candidates),
                       "top_k": top_k, "provider": reranker.name},
                output={"before": _brief(candidates, 3), "after": _brief(ranked, 5)},
            )
            hybrid = ranked
        rerank_info = {
            "provider": reranker.name,
            "applied": True,
            "is_fallback": reranker.is_fallback,
            "candidates": len(candidates),
        }

    took_ms = int((time.perf_counter() - started) * 1000)
    metrics.observe_hits("hybrid", len(hybrid))
    return {
        "vector": [_hit_to_dict(h) for h in vec_hits[:top_k]],
        "bm25": [_hit_to_dict(h) for h in bm_hits[:top_k]],
        "hybrid": [_hit_to_dict(h) for h in hybrid],
        "variants": variants,
        "degraded": degraded,
        "rerank": rerank_info,
        "took_ms": took_ms,
    }


def _hit_to_dict(h: Hit) -> dict:
    return {
        "chunk_id": h.chunk_id,
        "content": h.content,
        "score": round(h.score, 6),
        "rerank_score": h.rerank_score,
        "channel": h.channel,
        "channels": sorted(set(h.channels)),
        "doc_id": h.doc_id,
        "doc_name": h.doc_name,
        "doc_version_id": h.doc_version_id,
        "kb_id": h.kb_id,
        "biz_group": h.biz_group,
        "language": h.language,
        "page_no": h.page_no,
    }
