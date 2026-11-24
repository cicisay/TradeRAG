"""回答反馈 API（对应 PRD FR-405 / FR-803a：点赞、点踩 + 原因标签 + 自动快照）。

为什么要"点踩即快照"：
- 用户点踩时，文档可能过两天就更新了、会话也可能被清理；如果只存一个 message_id，
  事后回看时根本不知道当时检索到了什么、排序如何、回答长什么样。
- 因此点踩的瞬间就把：问题、回答、引用原文、检索元数据（通道/耗时/重排/变体）、
  request_id 一起固化到 badcase 里——这是排查与回归的证据链。
"""
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.db.session import get_db
from app.models.chat import ROLE_ASSISTANT, ROLE_USER, ChatSession, Message, RefSnapshot
from app.models.ops import FEEDBACK_REASONS, REASON_NAMES, Badcase, Feedback
from app.models.user import User
from app.services import metrics

router = APIRouter(prefix="/api/v1/feedback", tags=["feedback"])


class FeedbackIn(BaseModel):
    message_id: int
    rating: Literal["up", "down"]
    reason: str | None = Field(default=None, description="点踩必填：wrong_answer/incomplete/off_topic/bad_citation/too_slow/other")
    comment: str | None = Field(default=None, max_length=1000)


def _build_snapshot(db: Session, msg: Message, session_id: int) -> dict:
    refs = db.query(RefSnapshot).filter(RefSnapshot.message_id == msg.id).all()
    question = (
        db.query(Message)
        .filter(Message.session_id == session_id, Message.role == ROLE_USER, Message.id < msg.id)
        .order_by(Message.id.desc())
        .first()
    )
    try:
        retrieval = json.loads(msg.retrieval_meta_json) if msg.retrieval_meta_json else {}
    except json.JSONDecodeError:
        retrieval = {}
    return {
        "question": question.content if question else "",
        "answer": msg.content,
        "answer_language": msg.answer_language,
        "compliance_flag": msg.compliance_flag,
        "request_id": msg.request_id,
        "session_id": session_id,
        "message_id": msg.id,
        "prompt_version": "system-v1",
        "retrieval": retrieval,
        "refs": [
            {
                "doc_name": r.doc_name, "page_or_section": r.page_or_section,
                "score": r.score, "content_snapshot": (r.content_snapshot or "")[:500],
                "trans_snapshot": (r.trans_snapshot or "")[:500] if r.trans_snapshot else None,
            }
            for r in refs
        ],
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }


@router.post("")
def submit_feedback(
    payload: FeedbackIn,
    user: User = Depends(require_permission("chat:use")),
    db: Session = Depends(get_db),
) -> dict:
    """提交反馈。同一用户对同一消息重复提交时覆盖旧值（前端可改主意）。"""
    msg = db.get(Message, payload.message_id)
    if msg is None or msg.role != ROLE_ASSISTANT:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "回答不存在"})
    session = db.get(ChatSession, msg.session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=403, detail={"error_code": "E6003", "message": "只能评价自己的会话"})

    reason = payload.reason
    if payload.rating == "down":
        if reason not in FEEDBACK_REASONS:
            raise HTTPException(status_code=422, detail={
                "error_code": "E1001",
                "message": f"点踩必须选择原因：{', '.join(FEEDBACK_REASONS)}",
            })
    else:
        reason = None  # 点赞不需要原因

    snapshot = _build_snapshot(db, msg, session.id)
    feedback = (
        db.query(Feedback)
        .filter(Feedback.message_id == msg.id, Feedback.user_id == user.id)
        .first()
    )
    if feedback is None:
        feedback = Feedback(
            message_id=msg.id, user_id=user.id, rating=payload.rating,
            reason=reason, comment=payload.comment,
            snapshot_json=json.dumps(snapshot, ensure_ascii=False),
        )
        db.add(feedback)
        db.flush()
    else:
        feedback.rating = payload.rating
        feedback.reason = reason
        feedback.comment = payload.comment
        feedback.snapshot_json = json.dumps(snapshot, ensure_ascii=False)

    msg.feedback = payload.rating
    msg.feedback_reason = reason

    badcase_id = None
    existing = db.query(Badcase).filter(Badcase.feedback_id == feedback.id).first()
    if payload.rating == "down":
        if existing is None:
            badcase = Badcase(
                feedback_id=feedback.id, message_id=msg.id, session_id=session.id,
                user_id=user.id, query=snapshot["question"], answer=snapshot["answer"],
                reason=reason, status="pending", trace_id=msg.request_id,
                snapshot_json=feedback.snapshot_json,
            )
            db.add(badcase)
            db.flush()
            badcase_id = badcase.id
        else:
            existing.reason = reason
            existing.snapshot_json = feedback.snapshot_json
            if existing.status in ("archived", "closed"):
                existing.status = "pending"  # 再次点踩：重新打开工单
                existing.closed_at = None
            badcase_id = existing.id
    elif existing is not None and existing.status in ("pending", "located", "fixing"):
        # 用户撤销点踩：未处理的工单直接归档，避免污染质量看板
        existing.status = "archived"
        existing.note = ((existing.note or "") + "\n[系统] 用户已撤销点踩").strip()

    db.commit()
    metrics.record_feedback(payload.rating, reason)
    return {
        "ok": True, "message_id": msg.id, "rating": payload.rating,
        "reason": reason, "reason_name": REASON_NAMES.get(reason or "", None),
        "badcase_id": badcase_id,
    }
