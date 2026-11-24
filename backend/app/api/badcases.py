"""Badcase 管理 API（对应 PRD FR-803b/c/d、FR-804 知识缺口）。

闭环流程：
  点踩 → badcase(pending) → 归因/定位(located) → 修复(fixing) → 一键转评测样本(regressed)
  → 回归通过(closed) / 归档(archived)

权限：
- 查看与归因：audit:read（审计员/合规负责人/管理员）
- 转评测样本、修改状态：kb:manage（知识管理员/管理员）——写操作严格一点
"""
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.db.session import get_db
from app.models.ops import (
    ATTRIBUTIONS,
    ATTRIBUTION_NAMES,
    BAD_STATUSES,
    BAD_STATUS_NAMES,
    Badcase,
)
from app.models.user import User
from app.services.evaluation import EvalCase, append_badcase_sample

router = APIRouter(prefix="/api/v1/admin/badcases", tags=["badcases"])


def _out(row: Badcase, with_snapshot: bool = False) -> dict:
    data = {
        "id": row.id, "message_id": row.message_id, "session_id": row.session_id,
        "query": row.query, "answer": (row.answer or "")[:2000],
        "reason": row.reason, "status": row.status,
        "status_name": BAD_STATUS_NAMES.get(row.status, row.status),
        "attribution": row.attribution,
        "attribution_name": ATTRIBUTION_NAMES.get(row.attribution or "", None),
        "note": row.note, "trace_id": row.trace_id,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
    }
    if with_snapshot:
        try:
            data["snapshot"] = json.loads(row.snapshot_json or "{}")
        except json.JSONDecodeError:
            data["snapshot"] = {}
    return data


@router.get("/stats")
def badcase_stats(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("audit:read")),
) -> dict:
    """看板数据 + 7 日知识缺口榜（同一问题 ≥2 次点踩）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    by_status = {
        s: db.query(Badcase).filter(Badcase.status == s).count() for s in BAD_STATUSES
    }
    by_attribution = {
        (row[0] or "unset"): row[1]
        for row in db.query(Badcase.attribution, func.count(Badcase.id)).group_by(Badcase.attribution).all()
    }
    gaps = (
        db.query(Badcase.query, func.count(Badcase.id).label("cnt"))
        .filter(Badcase.created_at >= cutoff, Badcase.status != "archived")
        .group_by(Badcase.query)
        .having(func.count(Badcase.id) >= 2)
        .order_by(func.count(Badcase.id).desc())
        .limit(20)
        .all()
    )
    closed = db.query(Badcase).filter(Badcase.status == "closed", Badcase.closed_at.isnot(None)).all()
    avg_hours = None
    if closed:
        hours = [
            (c.closed_at - c.created_at).total_seconds() / 3600
            for c in closed if c.created_at and c.closed_at
        ]
        avg_hours = round(sum(hours) / len(hours), 1) if hours else None
    return {
        "by_status": by_status,
        "by_attribution": by_attribution,
        "open": sum(by_status[s] for s in ("pending", "located", "fixing")),
        "avg_close_hours": avg_hours,
        "gaps_7d": [{"query": q, "count": c} for q, c in gaps],
    }


@router.get("")
def list_badcases(
    status: str | None = None,
    attribution: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    with_snapshot: bool = False,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("audit:read")),
) -> dict:
    query = db.query(Badcase)
    if status:
        query = query.filter(Badcase.status == status)
    if attribution:
        query = query.filter(Badcase.attribution == attribution)
    total = query.count()
    rows = query.order_by(Badcase.id.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": [_out(r, with_snapshot) for r in rows]}


class BadcaseUpdate(BaseModel):
    status: str | None = None
    attribution: str | None = None
    note: str | None = Field(default=None, max_length=2000)


@router.get("/{badcase_id}")
def get_badcase(
    badcase_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("audit:read")),
) -> dict:
    row = db.get(Badcase, badcase_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "badcase 不存在"})
    return _out(row, with_snapshot=True)


@router.patch("/{badcase_id}")
def update_badcase(
    badcase_id: int,
    payload: BadcaseUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("kb:manage")),
) -> dict:
    row = db.get(Badcase, badcase_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "badcase 不存在"})
    if payload.status is not None:
        if payload.status not in BAD_STATUSES:
            raise HTTPException(status_code=422, detail={"error_code": "E1001", "message": f"status 仅支持 {BAD_STATUSES}"})
        row.status = payload.status
        row.closed_at = datetime.now(timezone.utc) if payload.status == "closed" else None
    if payload.attribution is not None:
        if payload.attribution not in ATTRIBUTIONS:
            raise HTTPException(status_code=422, detail={"error_code": "E1001", "message": f"attribution 仅支持 {ATTRIBUTIONS}"})
        row.attribution = payload.attribution
    if payload.note is not None:
        row.note = payload.note
    db.commit()
    db.refresh(row)
    return _out(row, with_snapshot=True)


class ToEvalIn(BaseModel):
    expected_docs: list[str] = Field(default_factory=list, description="修复后应命中的文档名（可留空，进草稿待人工补）")
    expected_keywords: list[str] = Field(default_factory=list)
    dataset: str = Field(default="draft_badcases", pattern=r"^[a-z0-9_]{3,40}$")
    lang: str = "zh"
    category: str = "badcase"
    note: str = ""


@router.post("/{badcase_id}/to-eval")
def badcase_to_eval(
    badcase_id: int,
    payload: ToEvalIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("kb:manage")),
) -> dict:
    """一键转评测样本（FR-803d）：写入草稿评测集，并把工单置为"待回归"。"""
    row = db.get(Badcase, badcase_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "badcase 不存在"})

    snapshot = {}
    try:
        snapshot = json.loads(row.snapshot_json or "{}")
    except json.JSONDecodeError:
        pass
    question = (snapshot.get("question") or row.query or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail={"error_code": "E1001", "message": "该工单没有问题文本，无法转评测样本"})

    case = EvalCase(
        id=f"B{badcase_id}", query=question,
        expected_docs=payload.expected_docs, expected_keywords=payload.expected_keywords,
        lang=payload.lang, category=payload.category,
        note=(payload.note or f"来自 badcase #{badcase_id}（{row.reason or '未标注原因'}）")[:500],
    )
    path = append_badcase_sample(case, dataset=payload.dataset)
    row.status = "regressed"
    row.note = ((row.note or "") + f"\n[系统] 已转入评测草稿 {path.name}，待补期望文档后并入正式集").strip()
    db.commit()
    return {
        "ok": True, "badcase_id": badcase_id, "dataset": payload.dataset,
        "file": path.name, "case_id": case.id,
        "message": "已写入评测草稿；补全 expected_docs 后执行 run_eval 即可回归",
        "operator": user.username,
    }
