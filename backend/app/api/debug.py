"""检索调试台与评测 API（PRD 9 /debug/retrieval、FR-700 五通道对照、FR-704 RBAC 模拟、
FR-705/706 批量评测）。

权限：仅管理员/安全审计员（FR-707）。管理员可用 simulate_user 以指定用户身份检索，
验证业务组隔离是否真的生效（越权内容 0 召回）。批量评测不调用生成模型，不消耗 Token。
"""
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.db.session import get_db
from app.models.ops import EvalRun
from app.models.user import User
from app.services.evaluation import list_datasets, run_eval
from app.services.retrieval import retrieve
from app.services import tracing
from app.services.sql_channel import route_question

router = APIRouter(prefix="/api/v1/debug", tags=["debug"])


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    kb_ids: list[int] | None = None
    top_k: int = Field(default=10, ge=1, le=50)
    crosslingual: bool = True
    rerank: bool = True
    simulate_user: str | None = None  # FR-704：以指定用户身份验证隔离（仅管理员可用）


def _effective_user(user: User, db: Session, simulate_user: str | None) -> User:
    """FR-704 RBAC 模拟：管理员可切换成任意用户身份跑检索/评测。"""
    if not simulate_user:
        return user
    if user.role.code != "admin":
        raise HTTPException(status_code=403, detail={"error_code": "E6003", "message": "仅管理员可模拟其他用户"})
    target = db.query(User).filter(User.username == simulate_user, User.status == "active").first()
    if target is None:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "被模拟用户不存在"})
    return target


@router.post("/retrieval")
def debug_retrieval(
    body: RetrievalRequest,
    user: User = Depends(require_permission("debug:retrieval")),
    db: Session = Depends(get_db),
) -> dict:
    started = time.perf_counter()
    effective_user = _effective_user(user, db, body.simulate_user)

    with tracing.trace(
        "debug.retrieval",
        user_id=user.id, input=body.query,
        metadata={"top_k": body.top_k, "simulate_user": body.simulate_user or ""},
    ) as lf:
        result = retrieve(
            db=db,
            user=effective_user,
            query=body.query,
            kb_ids=body.kb_ids,
            top_k=body.top_k,
            crosslingual=body.crosslingual,
            rerank=body.rerank,
            lf=lf,
        )
    # FR-701/703：SQL 通道也参与对照——只做"路由到哪个模板"，不在调试台执行写操作
    routed = route_question(body.query)
    sql_info = {"routed": bool(routed), "template_id": None, "params": {}}
    if routed:
        sql_info["template_id"], sql_info["params"] = routed
    result.update({
        "query": body.query,
        "sql": sql_info,
        "effective_user": {
            "username": effective_user.username,
            "role": effective_user.role.code if effective_user.role else "",
            "biz_group": effective_user.biz_group,
        },
        "server_took_ms": int((time.perf_counter() - started) * 1000),
    })
    return result


# --------------------------------------------------------------------------- #
# 批量评测（FR-705 / FR-706）
# --------------------------------------------------------------------------- #
class EvalRequest(BaseModel):
    dataset: str = Field(min_length=1, max_length=64)
    top_k: int = Field(default=5, ge=1, le=20)
    limit: int | None = Field(default=None, ge=1, le=500)
    crosslingual: bool | None = None
    rerank: bool = True
    rerank_top_n: int = Field(default=10, ge=5, le=50)
    include_details: bool = True
    simulate_user: str | None = None


@router.get("/eval/datasets")
def eval_datasets(
    _: User = Depends(require_permission("debug:retrieval")),
) -> list[dict]:
    """可用评测集：题量、语种分布、检索集/合规集。"""
    return list_datasets()


@router.post("/eval/run")
def eval_run(
    body: EvalRequest,
    user: User = Depends(require_permission("debug:retrieval")),
    db: Session = Depends(get_db),
) -> dict:
    """跑一个评测集。≤100 条约几十秒（重排候选 10）；结果写 eval_run 表并落 JSON 报告。"""
    effective_user = _effective_user(user, db, body.simulate_user)
    try:
        with tracing.trace(
            "eval.run", user_id=user.id, input=body.dataset,
            metadata={"top_k": body.top_k, "limit": body.limit, "simulate_user": body.simulate_user or ""},
        ):
            report = run_eval(
                db, effective_user, body.dataset,
                top_k=body.top_k, limit=body.limit,
                crosslingual=body.crosslingual, rerank=body.rerank,
                rerank_top_n=body.rerank_top_n, save=True,
            )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={"error_code": "E4004", "message": f"评测集不存在：{body.dataset}"})
    if not body.include_details:
        report.pop("details", None)
    return report


@router.get("/eval/runs")
def eval_runs(
    dataset: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("debug:retrieval")),
) -> list[dict]:
    """历史跑分（趋势对比用）。"""
    query = db.query(EvalRun)
    if dataset:
        query = query.filter(EvalRun.dataset == dataset)
    rows = query.order_by(EvalRun.id.desc()).limit(limit).all()
    return [
        {
            "id": r.id, "dataset": r.dataset, "cases": r.cases, "top_k": r.top_k,
            "recall": r.recall, "mrr": r.mrr, "ndcg": r.ndcg, "accuracy": r.accuracy,
            "duration_ms": r.duration_ms, "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]


@router.get("/eval/runs/{run_id}")
def eval_run_detail(
    run_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("debug:retrieval")),
) -> dict:
    row = db.get(EvalRun, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "E4004", "message": "评测记录不存在"})
    try:
        detail = json.loads(row.detail_json or "{}")
    except json.JSONDecodeError:
        detail = {}
    detail.update({
        "id": row.id, "dataset": row.dataset, "cases": row.cases,
        "recall": row.recall, "mrr": row.mrr, "ndcg": row.ndcg,
        "accuracy": row.accuracy, "duration_ms": row.duration_ms,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    })
    return detail
