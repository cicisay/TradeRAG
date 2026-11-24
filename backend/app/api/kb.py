"""知识库 / 版本 / 分片浏览 API（对应 PRD 9 的 documents 系列、FR-500 知识库页）。

为什么单独一个文件：
- 知识库是"隔离的最小单位"，它的增删查与业务组规则强相关，集中在一处更容易审查。
- 分片浏览是排查"检索为什么没命中"的第一现场（切分不当是最常见原因之一）。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.db.session import get_db
from app.models.chunk import Chunk
from app.models.document import DocVersion, Document
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User

router = APIRouter(prefix="/api/v1", tags=["knowledge-base"])


def _kb_visible(user: User, kb: KnowledgeBase) -> bool:
    return user.role.code == "admin" or kb.biz_group == user.biz_group


@router.get("/knowledge-bases")
def list_knowledge_bases(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("kb:read")),
) -> list[dict]:
    """知识库列表（自动按业务组过滤）+ 文档数/分片数，前端用来渲染上传目标。"""
    query = db.query(KnowledgeBase)
    if user.role.code != "admin":
        query = query.filter(KnowledgeBase.biz_group == user.biz_group)
    kbs = query.order_by(KnowledgeBase.id.asc()).all()

    doc_counts = dict(
        db.query(Document.kb_id, func.count(Document.id)).group_by(Document.kb_id).all()
    )
    chunk_counts = dict(
        db.query(Document.kb_id, func.count(Chunk.id))
        .join(DocVersion, DocVersion.id == Chunk.doc_version_id)
        .join(Document, Document.id == DocVersion.doc_id)
        .group_by(Document.kb_id)
        .all()
    )
    return [
        {
            "id": kb.id, "name": kb.name, "biz_group": kb.biz_group,
            "product_line": kb.product_line, "embedding_model": kb.embedding_model,
            "documents": doc_counts.get(kb.id, 0), "chunks": chunk_counts.get(kb.id, 0),
            "created_by": kb.created_by,
            "created_at": kb.created_at.isoformat() if kb.created_at else "",
        }
        for kb in kbs
    ]


class KnowledgeBaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    biz_group: str | None = Field(default=None, max_length=64)
    product_line: str = Field(default="general", max_length=64)


@router.post("/knowledge-bases", status_code=201)
def create_knowledge_base(
    payload: KnowledgeBaseIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("kb:manage")),
) -> dict:
    """新建知识库。非管理员只能建自己业务组的（防止越权标注，FR-603）。"""
    if db.query(KnowledgeBase).filter(KnowledgeBase.name == payload.name).first():
        raise HTTPException(status_code=409, detail={"error_code": "E1001", "message": "知识库名称已存在"})
    biz_group = payload.biz_group if user.role.code == "admin" and payload.biz_group else user.biz_group
    kb = KnowledgeBase(
        name=payload.name, biz_group=biz_group,
        product_line=payload.product_line, created_by=user.username,
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return {"id": kb.id, "name": kb.name, "biz_group": kb.biz_group, "product_line": kb.product_line}


def _doc_or_403(doc_id: int, user: User, db: Session) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "文档不存在"})
    kb = db.get(KnowledgeBase, doc.kb_id)
    if kb is None or not _kb_visible(user, kb):
        raise HTTPException(status_code=403, detail={"error_code": "E6003", "message": "没有该文档的访问权限"})
    return doc


@router.get("/documents/{doc_id}/versions")
def list_versions(
    doc_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("kb:read")),
) -> list[dict]:
    """版本链（FR-109 版本归档：旧版本保留，不覆盖）。"""
    doc = _doc_or_403(doc_id, user, db)
    versions = (
        db.query(DocVersion).filter(DocVersion.doc_id == doc.id)
        .order_by(DocVersion.version_no.desc()).all()
    )
    return [
        {
            "id": v.id, "version_no": v.version_no, "file_size": v.file_size,
            "checksum": v.checksum[:16], "parse_status": v.parse_status,
            "page_count": v.page_count, "ocr_used": v.ocr_used,
            "ocr_confidence": v.ocr_confidence, "ocr_low_pages": v.ocr_low_pages or [],
            "current": v.id == doc.current_version_id,
            "uploaded_at": v.uploaded_at.isoformat() if v.uploaded_at else "",
        }
        for v in versions
    ]


@router.get("/doc-versions/{version_id}/chunks")
def list_chunks(
    version_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("kb:read")),
) -> dict:
    """分片浏览：查"这段内容被切成什么样"，是排查召回问题的第一现场。"""
    version = db.get(DocVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "版本不存在"})
    _doc_or_403(version.doc_id, user, db)

    query = db.query(Chunk).filter(Chunk.doc_version_id == version_id)
    total = query.count()
    rows = query.order_by(Chunk.chunk_index.asc()).offset(offset).limit(limit).all()
    return {
        "total": total, "offset": offset, "limit": limit,
        "items": [
            {
                "id": c.id, "chunk_index": c.chunk_index, "content": c.content,
                "language": c.language, "page_no": c.page_no,
                "ocr_confidence": c.ocr_confidence, "status": c.status,
            }
            for c in rows
        ],
    }
