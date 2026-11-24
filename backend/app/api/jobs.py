"""任务 API（M6 步骤10-2）：进度查询、重试、取消、队列概览。

权限设计：
- 任务台账只对"能管知识库"的人开放（kb:manage）。普通销售看不到别人上传了什么、
  文件多大、失败原因是什么——这些都属于运营信息，不是业务信息。
- 业务组隔离仍然生效：知识库管理员只能看到自己业务组文档的任务（管理员看全部）。

前端用法（knowledge.js）：
1. 上传返回 job_id → 每 1.5s GET /api/v1/jobs/{id} 直到 terminal；
2. 任务面板每 8s 拉一次 /api/v1/jobs?active=true；
3. 失败任务可点"重试"（复用同一条台账，attempts 清零重排）。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.db.session import get_db
from app.models.document import Document
from app.models.jobs import (
    STAGE_CANCELED, STAGE_FAILED, STAGE_NAMES, STAGE_QUEUED, STAGE_RUNNING, IngestJob,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.services.ingest import cancel_job, enqueue_ingest, job_to_dict, queue_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def _scoped_query(db: Session, user: User):
    """按业务组收窄任务范围（管理员看全部）。"""
    query = db.query(IngestJob, Document.doc_name).outerjoin(Document, Document.id == IngestJob.doc_id)
    if user.role.code != "admin":
        query = query.join(KnowledgeBase, KnowledgeBase.id == Document.kb_id).filter(
            KnowledgeBase.biz_group == user.biz_group
        )
    return query


def _get_scoped(db: Session, user: User, job_id: int) -> tuple[IngestJob, str | None]:
    row = _scoped_query(db, user).filter(IngestJob.id == job_id).first()
    if row is None:
        # 越权与不存在统一返回"不存在"，不泄露资源存在性
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "任务不存在"})
    return row[0], row[1]


@router.get("/stats")
def job_stats(
    user: User = Depends(require_permission("kb:manage")),
    db: Session = Depends(get_db),
) -> dict:
    """队列概览：模式、broker 可用性、各阶段任务数、今日完成量、平均耗时。"""
    return queue_status(db)


@router.get("")
def list_jobs(
    status_filter: str | None = Query(default=None, alias="status"),
    active: bool = False,
    doc_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_permission("kb:manage")),
    db: Session = Depends(get_db),
) -> dict:
    """任务列表。active=true 只看"排队中 + 进行中"，用于前端小面板轮询。"""
    query = _scoped_query(db, user)
    if active:
        query = query.filter(IngestJob.stage.in_((STAGE_QUEUED, *STAGE_RUNNING)))
    elif status_filter:
        if status_filter not in STAGE_NAMES:
            raise HTTPException(status_code=422, detail={"error_code": "E1001", "message": "未知的任务状态"})
        query = query.filter(IngestJob.stage == status_filter)
    if doc_id:
        query = query.filter(IngestJob.doc_id == doc_id)

    total = query.count()
    rows = query.order_by(IngestJob.id.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "items": [job_to_dict(job, name) for job, name in rows],
    }


@router.get("/{job_id}")
def get_job(
    job_id: int,
    user: User = Depends(require_permission("kb:manage")),
    db: Session = Depends(get_db),
) -> dict:
    job, doc_name = _get_scoped(db, user, job_id)
    return job_to_dict(job, doc_name)


@router.post("/{job_id}/retry")
def retry_job(
    job_id: int,
    user: User = Depends(require_permission("kb:manage")),
    db: Session = Depends(get_db),
) -> dict:
    """重试：复用同一条台账（attempts 清零），不新建记录，避免历史里出现一堆重复任务。"""
    job, doc_name = _get_scoped(db, user, job_id)
    if job.stage in (STAGE_QUEUED, *STAGE_RUNNING):
        raise HTTPException(status_code=409, detail={"error_code": "E1002", "message": "任务正在处理中，无需重试"})
    doc = db.get(Document, job.doc_id)
    from app.models.document import DocVersion

    version = db.get(DocVersion, job.doc_version_id)
    if doc is None or version is None:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "文档或版本已被删除"})
    new_job, _ = enqueue_ingest(
        db, doc, version, created_by=user.username, request_id=job.request_id, force=True
    )
    logger.info("任务手动重试 job=%s by=%s", job_id, user.username)
    return {"ok": True, "job": job_to_dict(new_job, doc_name)}


@router.post("/{job_id}/cancel")
def cancel(
    job_id: int,
    user: User = Depends(require_permission("kb:manage")),
    db: Session = Depends(get_db),
) -> dict:
    """取消：置 canceled + 尽力撤销队列消息。正在跑的批次结束后生效（协作式）。"""
    job, _ = _get_scoped(db, user, job_id)
    if job.stage == STAGE_CANCELED:
        return {"canceled": True, "already": True, "job": job_to_dict(job)}
    result = cancel_job(db, job)
    if not result.get("canceled"):
        raise HTTPException(status_code=409, detail={"error_code": "E1002", "message": result.get("reason", "无法取消")})
    logger.info("任务取消 job=%s by=%s", job_id, user.username)
    return result


@router.get("/{job_id}/error")
def job_error(
    job_id: int,
    user: User = Depends(require_permission("kb:manage")),
    db: Session = Depends(get_db),
) -> dict:
    """单独取错误详情（列表里只放截断的错误，完整堆栈按需拉取）。"""
    job, doc_name = _get_scoped(db, user, job_id)
    return {
        "job_id": job.id,
        "doc_name": doc_name,
        "stage": job.stage,
        "attempts": job.attempts,
        "error": job.error,
        "message": job.message,
    }
