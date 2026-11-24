"""监控与质量看板 API（对应 PRD 5.8.1 / 9：/monitor/summary、Prometheus 抓取）。

三个出口：
- `/metrics`：Prometheus 文本格式，运维侧直接抓取；不放在 /api/v1 下（抓取器习惯）。
- `/api/v1/monitor/summary`：给前端质量看板用的 JSON（漏斗、比率、延迟分位、业务计数）。
- 后台线程 `_bridge_loop`：每 30s 从 PG 刷业务存量 → Prometheus Gauge（DB 桥接层）。
  为什么不用 pushgateway？进程内 Gauge + Prometheus 拉取更简单，多副本各自刷、
  PromQL `sum` 就能聚合；而且 Gauge 是"当前值"不是"累计值"，pushgateway 会残留旧样本。
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import distinct, func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.deps import require_permission
from app.db.session import SessionLocal, get_db
from app.models.chat import ROLE_USER, ChatSession, Message
from app.models.chunk import Chunk
from app.models.compliance import ComplianceEvent
from app.models.document import PARSE_DONE, Document, DocumentMeta, DocVersion
from app.models.ops import BAD_STATUS_NAMES, ATTRIBUTIONS, Badcase, EvalRun
from app.models.sql_channel import SqlExecAudit
from app.models.user import User
from app.services import metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/monitor", tags=["monitor"])
metrics_router = APIRouter(tags=["monitor"])

_bridge_stop = threading.Event()
_bridge_thread: threading.Thread | None = None


def start_metrics_bridge() -> None:
    """启动 DB → Prometheus Gauge 的桥接线程（由 main.py lifespan 调）。

    幂等：已经在跑就不重复起。关闭由进程退出处理（daemon=True）。
    """
    global _bridge_thread
    if _bridge_thread is not None and _bridge_thread.is_alive():
        return
    if not get_settings().metrics_enabled:
        logger.info("metrics 未启用，跳过 DB 桥接")
        return
    _bridge_stop.clear()
    _bridge_thread = threading.Thread(
        target=_bridge_loop, name="metrics-bridge", daemon=True,
    )
    _bridge_thread.start()
    logger.info("DB → Prometheus 桥接线程已启动（30s 刷新）")


def _bridge_loop() -> None:
    """每 30s 刷一次。单次查询超时/异常不影响下一轮（所有异常都捕获）。"""
    while not _bridge_stop.wait(30.0):
        try:
            _refresh_once()
        except Exception:  # noqa: BLE001 - 桥接线程不能因为一次失败就退出
            logger.exception("metrics bridge 刷新失败，下一轮继续")


def _refresh_once() -> None:
    """一次完整刷新（含所有需要的 DB 查询）。"""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        day_ago = now - timedelta(days=1)
        week_ago = now - timedelta(days=7)

        # --- 知识库库存 ---
        doc_total = db.query(Document).count()
        chunk_total = db.query(Chunk).count()
        # 过期文档：DocumentMeta.expire_date 已过（检索侧不按时效过滤，这里只做统计暴露）
        doc_expired = (
            db.query(func.count(Document.id))
            .join(DocumentMeta, DocumentMeta.doc_id == Document.id)
            .filter(
                DocumentMeta.expire_date.isnot(None),
                DocumentMeta.expire_date < now.date(),
            )
            .scalar() or 0
        )
        # 版本不一致：同一文档有 ≥2 个解析成功的版本。
        # 检索只过滤 Document.status，不按 current_version_id 收口，旧版 chunk 仍可能被召回。
        inconsistent = (
            db.query(DocVersion.doc_id)
            .filter(DocVersion.parse_status == PARSE_DONE)
            .group_by(DocVersion.doc_id)
            .having(func.count(DocVersion.id) > 1)
            .count()
        )
        metrics.set_kb_inventory(
            documents=doc_total, chunks=chunk_total,
            expired=doc_expired, versions_inconsistent=inconsistent,
        )

        # --- 活跃度 ---
        # DAU：有消息的 distinct user_id（通过 session → user）
        dau = (
            db.query(func.count(distinct(ChatSession.user_id)))
            .join(Message, Message.session_id == ChatSession.id)
            .filter(Message.created_at >= day_ago)
            .scalar() or 0
        )
        wau = (
            db.query(func.count(distinct(ChatSession.user_id)))
            .join(Message, Message.session_id == ChatSession.id)
            .filter(Message.created_at >= week_ago)
            .scalar() or 0
        )
        # 24h 人均提问
        msgs_24h = db.query(Message).filter(
            Message.created_at >= day_ago, Message.role == ROLE_USER,
        ).count()
        ppu = round(msgs_24h / dau, 2) if dau else 0.0
        # 24h 问题重复率：内容完全相同且出现 >1 次的提问占比
        try:
            repeat_subq = (
                db.query(Message.content.label("q"), func.count(Message.id).label("cnt"))
                .filter(Message.created_at >= day_ago, Message.role == ROLE_USER)
                .group_by(Message.content)
                .having(func.count(Message.id) > 1)
                .subquery()
            )
            repeated_total = db.query(func.sum(repeat_subq.c.cnt)).scalar() or 0
            repeat_ratio = round(repeated_total / msgs_24h, 4) if msgs_24h else 0.0
        except Exception:  # noqa: BLE001 - 单条统计失败不能污染后续查询
            db.rollback()
            repeat_ratio = 0.0
        metrics.set_active_users(
            daily=dau, weekly=wau,
            questions_per_user_24h=ppu,
            repeat_ratio_24h=repeat_ratio,
        )

        # --- badcase 存量 ---
        by_status = {
            s: db.query(Badcase).filter(Badcase.status == s).count()
            for s in BAD_STATUS_NAMES
        }
        open_count = db.query(Badcase).filter(
            Badcase.status.in_(("pending", "located", "fixing")),
        ).count()
        by_attr = {
            row[0] or "unset": row[1]
            for row in db.query(Badcase.attribution, func.count(Badcase.id))
            .group_by(Badcase.attribution).all()
        }
        metrics.set_badcase_gauges(
            open_count=open_count, by_status=by_status, by_attribution=by_attr,
        )

        # --- 最新评测（每个 dataset 取最新一条 push 到 Gauge）---
        eval_rows = (
            db.query(EvalRun)
            .order_by(EvalRun.dataset, EvalRun.id.desc())
            .all()
        )
        seen: set[str] = set()
        for row in eval_rows:
            if row.dataset in seen:
                continue
            seen.add(row.dataset)
            if row.dataset.startswith("compliance"):
                # 合规集：accuracy 才有意义，recall/mrr/ndcg 为空
                metrics.push_eval_result(
                    row.dataset,
                    compliance_accuracy=(
                        float(row.accuracy) if row.accuracy is not None else None
                    ),
                )
            else:
                # 检索集：recall/mrr/ndcg 有效，accuracy 恒 0 不可当作合规准确率
                metrics.push_eval_result(
                    row.dataset,
                    recall=float(row.recall) if row.recall is not None else None,
                    mrr=float(row.mrr) if row.mrr is not None else None,
                    ndcg=float(row.ndcg) if row.ndcg is not None else None,
                )
            # Faithfulness / pass_rate 暂未存 EvalRun，等 run_eval.py 扩展后补齐
            # 评测脚本直接调 metrics.push_eval_result 即可

    finally:
        db.close()


# ---------------- 路由 ----------------

@metrics_router.get("/metrics", include_in_schema=False)
def prometheus_metrics(authorization: str | None = Header(default=None)) -> PlainTextResponse:
    """Prometheus 抓取端点。配置了 metrics_token 时需带 Bearer 令牌（避免指标外泄）。"""
    token = get_settings().metrics_token
    if token and authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail={"error_code": "E6001", "message": "metrics token 无效"})
    return PlainTextResponse(metrics.render_prometheus(), media_type="text/plain; version=0.0.4")


@router.get("/summary")
def monitor_summary(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("audit:read")),
) -> dict:
    """质量看板：进程指标 + 业务存量 + 最近评测。"""
    snap = metrics.snapshot()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    latest_eval = (
        db.query(EvalRun).order_by(EvalRun.id.desc()).first()
    )
    badcase_by_status = {
        status: db.query(Badcase).filter(Badcase.status == status).count()
        for status in BAD_STATUS_NAMES
    }
    return {
        "metrics": snap,
        "inventory": {
            "documents": db.query(Document).count(),
            "chunks": db.query(Chunk).count(),
            "sessions": db.query(ChatSession).count(),
            "messages": db.query(Message).count(),
        },
        "today": {
            "compliance_events": db.query(ComplianceEvent).filter(ComplianceEvent.created_at >= today).count(),
            "sql_execs": db.query(SqlExecAudit).filter(SqlExecAudit.created_at >= today).count(),
            "sql_denied": db.query(SqlExecAudit).filter(
                SqlExecAudit.created_at >= today, SqlExecAudit.status == "denied"
            ).count(),
            "badcases": db.query(Badcase).filter(Badcase.created_at >= today).count(),
        },
        "badcases": {
            "by_status": badcase_by_status,
            "open": db.query(Badcase).filter(Badcase.status.in_(("pending", "located", "fixing"))).count(),
            "week_new": db.query(Badcase).filter(Badcase.created_at >= week_ago).count(),
            "attribution": {
                row[0] or "unset": row[1]
                for row in db.query(Badcase.attribution, func.count(Badcase.id))
                .group_by(Badcase.attribution).all()
            },
        },
        "latest_eval": None if latest_eval is None else {
            "id": latest_eval.id, "dataset": latest_eval.dataset,
            "cases": latest_eval.cases, "recall": latest_eval.recall,
            "mrr": latest_eval.mrr, "ndcg": latest_eval.ndcg,
            "accuracy": latest_eval.accuracy, "duration_ms": latest_eval.duration_ms,
            "created_at": latest_eval.created_at.isoformat() if latest_eval.created_at else "",
            "created_by": latest_eval.created_by,
        },
    }


@router.get("/alerts")
def monitor_alerts(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("audit:read")),
    window: int = Query(300, ge=60, le=3600, description="观察窗口（秒）"),
) -> dict:
    """按 PRD 5.8.1 的阈值给出"当前是否告警"。

    说明：进程指标是自启动累计值，这里用窗口内增量做近似判断；
    生产接 Prometheus 后应以 PromQL 的 rate() 为准（本接口适合演示/小规模）。
    """
    snap = metrics.snapshot()
    rates = snap["rates"]
    alerts: list[dict] = []

    def add(level: str, code: str, message: str) -> None:
        alerts.append({"level": level, "code": code, "message": message})

    completion = rates.get("completion")
    if completion is not None and completion < 0.97:
        add("P1", "completion_low", f"端到端完成率 {completion:.1%} < 97%")
    empty = rates.get("empty_answer")
    if empty is not None and empty > 0.15:
        add("P2", "empty_high", f"空答率 {empty:.1%} > 15%")
    p95 = snap["latency_ms"].get("first_token", {}).get("p95", 0)
    if p95 and p95 > 3750:
        add("P2", "first_token_slow", f"首 Token P95 {p95:.0f}ms > 3750ms")
    blocked = snap["compliance"].get("block", 0)
    if blocked and snap["answers"]["total"] and blocked >= snap["answers"]["total"]:
        add("P0", "compliance_all_blocked", "全部请求被合规拦截，请检查规则是否误配")
    return {
        "window_seconds": window,
        "alerts": alerts,
        "healthy": not any(a["level"] == "P0" for a in alerts),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
