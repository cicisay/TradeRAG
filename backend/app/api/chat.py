"""聊天 API（对应 PRD 9：POST /chat SSE、GET /sessions、/sessions/{id}/messages）。

SSE（Server-Sent Events）格式：
  event: meta
  data: {...}

  event: delta
  data: {"content": "..."}

  event: done
  data: {...}
前端用 EventSource/fetch 逐条读取即可实现打字机效果（FR-209）。
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.chat import ChatSession, Message, RefSnapshot
from app.models.user import User

logger = logging.getLogger(__name__)
from app.services.chat import stream_answer

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)  # PRD 7.3 输入长度上限
    session_id: int | None = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat")
def chat(
    body: ChatRequest,
    request: Request,
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """流式问答。注意：这里不注入请求级 db（见 chat.stream_answer 说明），只传 user_id。"""
    user_id = user.id
    client_ip = request.client.host if request.client else None

    def event_stream():
        for event, data in stream_answer(user_id, body.query, body.session_id, ip=client_ip):
            yield _sse(event, data)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关闭 Nginx 缓冲，否则流式会被攒成一整包
            "Connection": "keep-alive",
        },
    )


@router.get("/sessions")
def list_sessions(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict]:
    """我的会话列表（FR-401）。"""
    sessions = (
        db.query(ChatSession)
        .filter(ChatSession.user_id == user.id)
        .order_by(ChatSession.updated_at.desc())
        .limit(100)
        .all()
    )
    result = []
    for s in sessions:
        count = db.query(Message).filter(Message.session_id == s.id).count()
        result.append({
            "id": s.id, "title": s.title, "updated_at": s.updated_at.isoformat(),
            "message_count": count,
        })
    return result


@router.get("/sessions/{session_id}/messages")
def session_messages(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """会话历史（含引用快照）。只能看自己的会话（FR-605）。"""
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "会话不存在"})
    messages = (
        db.query(Message)
        .filter(Message.session_id == session_id)
        .order_by(Message.id.asc())
        .all()
    )
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "answer_language": m.answer_language,
            "compliance_flag": m.compliance_flag,
            "request_id": m.request_id,
            "feedback": m.feedback,
            "feedback_reason": m.feedback_reason,
            "retrieval": json.loads(m.retrieval_meta_json) if m.retrieval_meta_json else None,
            "created_at": m.created_at.isoformat(),
            "refs": [
                {
                    "doc_name": r.doc_name,
                    "content_snapshot": r.content_snapshot,
                    "trans_snapshot": r.trans_snapshot,
                    "page_or_section": r.page_or_section,
                    "score": r.score,
                }
                for r in db.query(RefSnapshot).filter(RefSnapshot.message_id == m.id).all()
            ],
        }
        for m in messages
    ]


class RenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=60)


@router.patch("/sessions/{session_id}")
def rename_session(
    session_id: int,
    body: RenameIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """重命名会话。只能改自己的（FR-605）。

    标题默认取首问前 30 字，问得多了列表里全是"不锈钢法兰的材质和压力等级有…"，
    认不出来，所以给用户一个改名的口子。
    """
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "会话不存在"})
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail={"error_code": "E6006", "message": "标题不能为空"})
    session.title = title[:60]
    db.commit()
    logger.info("会话已重命名 session_id=%s user=%s", session_id, user.username)
    return {"ok": True, "id": session.id, "title": session.title}


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """删除会话及其全部消息与引用快照。只能删自己的（FR-605）。

    删除顺序：先按 message_id 清引用快照，再删消息，最后删会话本体。
    手动级联是因为 ref_snapshot.message_id 没配 ON DELETE CASCADE，
    直接删会话会被外键拦住（SQLite 开了 PRAGMA foreign_keys）。
    """
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "会话不存在"})
    msg_ids = [mid for (mid,) in db.query(Message.id).filter(Message.session_id == session_id).all()]
    if msg_ids:
        db.query(RefSnapshot).filter(RefSnapshot.message_id.in_(msg_ids)).delete(synchronize_session=False)
    db.query(Message).filter(Message.session_id == session_id).delete(synchronize_session=False)
    db.delete(session)
    db.commit()
    logger.info("会话已删除 session_id=%s user=%s 消息数=%s", session_id, user.username, len(msg_ids))
    return {"ok": True, "deleted_messages": len(msg_ids)}
