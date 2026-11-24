"""文档 API：上传 / 列表 / 详情（对应 PRD 9：/documents/upload、/documents）。

安全要点（PRD 7.2 上传校验）：
1. 类型白名单（扩展名 + **魔数**双重校验——改后缀名骗不过魔数检查）
2. ≤100MB（MAX_UPLOAD_MB，超限 413）
3. Word 含宏（vbaProject.bin）即拒；宏是常见恶意载荷入口
4. 业务组越权校验：只能传到自己业务组的知识库（FR-603）
5. biz_group/密级标注取自知识库与受控参数，不由客户端随意指定
"""
import hashlib
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.config import BASE_DIR, get_settings
from app.core.deps import get_current_user, require_permission
from app.db.session import get_db
from app.models.document import (
    PARSE_PENDING, STATUS_UPLOADED, AUTHORITY_LEVELS, DOC_TYPES, DocVersion, Document, DocumentMeta,
)
from app.models.jobs import STAGE_CANCELED, STAGE_FAILED, STAGE_INDEXED
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.schemas.document import DocumentOut
from app.services.ingest import enqueue_ingest
from app.services.parser import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

# 魔数表：文件头真实字节（PRD 7.2 魔数校验）
MAGIC_NUMBERS = {
    ".pdf": b"%PDF-",
    ".docx": b"PK\x03\x04",  # OOXML 本质是 zip
    ".xlsx": b"PK\x03\x04",
}


def _check_magic(ext: str, data: bytes) -> str | None:
    """魔数不符返回错误描述，符合返回 None。txt/md 无固定魔数，跳过。"""
    magic = MAGIC_NUMBERS.get(ext)
    if magic and not data.startswith(magic):
        return f"文件内容与扩展名 {ext} 不符（魔数校验失败），疑似伪装文件"
    return None


def _check_office_macro(ext: str, data: bytes) -> str | None:
    """Word/Excel 宏检测：zip 包内含 vbaProject.bin 即拒（PRD 7.2：含宏即拒）。"""
    if ext in (".docx", ".xlsx"):
        import zipfile
        import io
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if any(name.startswith("word/vbaProject.bin") or name.startswith("xl/vbaProject.bin") for name in zf.namelist()):
                    return "文件包含宏（vbaProject.bin），按安全策略拒绝入库"
        except zipfile.BadZipFile:
            return "OOXML 文件结构损坏（非合法 zip 容器）"
    return None


def _resolve_kb(kb_id: int, user: User, db: Session) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "知识库不存在"})
    # FR-603：非管理员只能操作自己业务组的知识库；批量探测统一"无权限"（PRD 7.6）
    if user.role.code != "admin" and kb.biz_group != user.biz_group:
        raise HTTPException(status_code=403, detail={"error_code": "E6003", "message": "没有该知识库的操作权限"})
    return kb


@router.post("/upload", response_model=DocumentOut)
def upload_document(
    response: Response,
    kb_id: int = Form(...),
    file: UploadFile = File(...),
    doc_type: str = Form("other"),
    authority_level: int = Form(2),
    languages: str = Form(""),  # 逗号分隔，如 "zh,en"
    user: User = Depends(require_permission("kb:manage")),
    db: Session = Depends(get_db),
) -> DocumentOut:
    """上传文档：落盘 + 登记 + 入队后立即返回（默认 202）。

    给新手的说明：以前这个接口会同步跑完 OCR 和嵌入，大文件要等几分钟；
    现在只做"收件"，真正的处理在后台任务里，前端拿 job_id 轮询进度。
    同步模式（INGEST_MODE=inline，测试与排障用）仍会一次做完，返回 200。
    """
    settings = get_settings()
    # ---- 参数校验 ----
    if doc_type not in DOC_TYPES:
        raise HTTPException(status_code=422, detail={"error_code": "E1001", "message": f"doc_type 仅支持：{', '.join(DOC_TYPES)}"})
    if authority_level not in AUTHORITY_LEVELS:
        raise HTTPException(status_code=422, detail={"error_code": "E1001", "message": "authority_level 取值 0-3（0=T0 最高权威）"})

    kb = _resolve_kb(kb_id, user, db)

    # ---- 读取文件并做安全校验 ----
    max_bytes = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail={"error_code": "E1001", "message": f"文件超过 {settings.max_upload_mb}MB 上限"})
    if not data:
        raise HTTPException(status_code=422, detail={"error_code": "E1001", "message": "文件内容为空"})

    filename = Path(file.filename or "").name  # 防路径穿越：只取文件名部分
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=422, detail={"error_code": "E1001", "message": f"不支持的文件类型 {ext}，白名单：{', '.join(sorted(SUPPORTED_EXTENSIONS))}"})

    err = _check_magic(ext, data) or _check_office_macro(ext, data)
    if err:
        logger.warning("上传安全拦截 user=%s file=%s reason=%s", user.username, filename, err)
        raise HTTPException(status_code=422, detail={"error_code": "E1001", "message": err})

    # ---- 登记 document / doc_version / document_meta ----
    doc = Document(kb_id=kb.id, doc_name=filename, status=STATUS_UPLOADED, created_by=user.username)
    db.add(doc)
    db.flush()  # 拿到 doc.id，供版本记录与文件落盘路径使用

    checksum = hashlib.sha256(data).hexdigest()
    rel_dir = Path("files") / str(kb.id) / str(doc.id)
    version = DocVersion(
        doc_id=doc.id, version_no=1,
        file_path=str(rel_dir / f"v1_{filename}"),
        file_size=len(data), checksum=checksum,
        parse_status=PARSE_PENDING,
    )
    db.add(version)
    db.flush()

    doc.current_version_id = version.id
    db.add(DocumentMeta(
        doc_id=doc.id,
        languages=[lang.strip().lower() for lang in languages.split(",") if lang.strip()],
        biz_group=kb.biz_group,  # 业务组随知识库，防越权标注
        doc_type=doc_type,
        authority_level=authority_level,
    ))

    # ---- 落盘原始文件 ----
    abs_path = BASE_DIR / version.file_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(data)

    # ---- 先提交登记信息，再入队（worker 是另一个进程/会话，必须能读到这条记录）----
    db.commit()

    # ---- 交给异步流水线（M6 步骤10-2）----
    import uuid

    request_id = uuid.uuid4().hex[:16]
    try:
        job, _ = enqueue_ingest(
            db, doc, version,
            created_by=user.username, request_id=request_id,
        )
    except Exception as exc:
        # 入队失败不能"静默成功"：文档保持 uploaded，用户可重试或由管理员重建索引
        logger.exception("入队失败 doc_id=%s", doc.id)
        raise HTTPException(
            status_code=503,
            detail={"error_code": "E5001", "message": f"任务入队失败，请稍后重试（文档已保存）：{str(exc)[:120]}"},
        ) from exc

    db.refresh(doc)
    db.refresh(version)

    chunks_indexed = None
    if job.result_json:
        try:
            chunks_indexed = int(json.loads(job.result_json).get("chunks") or 0)
        except (ValueError, TypeError):
            chunks_indexed = None

    # 只有仍在处理才算排队；failed / canceled 是终态，不该让前端继续轮询
    queued = job.stage not in (STAGE_INDEXED, STAGE_FAILED, STAGE_CANCELED)
    if queued:
        response.status_code = status.HTTP_202_ACCEPTED
    logger.info(
        "上传成功 doc_id=%s file=%s job=%s mode=%s stage=%s",
        doc.id, filename, job.id, job.broker, job.stage,
    )

    meta = doc.meta
    return DocumentOut(
        id=doc.id, kb_id=doc.kb_id, doc_name=doc.doc_name, status=doc.status,
        current_version_id=doc.current_version_id,
        doc_type=meta.doc_type if meta else None,
        languages=meta.languages if meta else None,
        authority_level=meta.authority_level if meta else None,
        biz_group=meta.biz_group if meta else None,
        page_count=version.page_count, parse_status=version.parse_status,
        chunks=chunks_indexed,
        ocr_used=version.ocr_used, ocr_confidence=version.ocr_confidence,
        ocr_low_pages=version.ocr_low_pages or [], ocr_error=version.ocr_error,
        job_id=job.id, job_stage=job.stage, queue_mode=job.broker, queued=queued,
    )


@router.get("", response_model=list[DocumentOut])
def list_documents(
    kb_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DocumentOut]:
    """文档列表：自动按业务组过滤（FR-603）。普通用户只能看到自己业务组的文档。"""
    query = db.query(Document).join(DocumentMeta, DocumentMeta.doc_id == Document.id)
    if user.role.code == "admin":
        query = db.query(Document)  # 管理员可见全部
    else:
        query = query.join(KnowledgeBase, KnowledgeBase.id == Document.kb_id).filter(
            KnowledgeBase.biz_group == user.biz_group
        )
    if kb_id:
        query = query.filter(Document.kb_id == kb_id)
    docs = query.order_by(Document.id.desc()).limit(200).all()
    jobs = _latest_jobs(db, [d.current_version_id for d in docs if d.current_version_id])
    return [
        DocumentOut(
            id=d.id, kb_id=d.kb_id, doc_name=d.doc_name, status=d.status,
            current_version_id=d.current_version_id,
            doc_type=d.meta.doc_type if d.meta else None,
            languages=d.meta.languages if d.meta else None,
            authority_level=d.meta.authority_level if d.meta else None,
            biz_group=d.meta.biz_group if d.meta else None,
            job_id=jobs[d.current_version_id].id if d.current_version_id in jobs else None,
            job_stage=jobs[d.current_version_id].stage if d.current_version_id in jobs else None,
            queued=(
                jobs[d.current_version_id].stage not in ("indexed", "failed", "canceled")
                if d.current_version_id in jobs else False
            ),
        )
        for d in docs
    ]


def _latest_jobs(db: Session, version_ids: list[int]) -> dict[int, object]:
    """一次查出这批文档各自最新的任务（避免 N+1 查询）。"""
    if not version_ids:
        return {}
    from app.models.jobs import IngestJob

    rows = (
        db.query(IngestJob)
        .filter(IngestJob.doc_version_id.in_(version_ids))
        .order_by(IngestJob.id.desc())
        .all()
    )
    latest: dict[int, object] = {}
    for job in rows:
        latest.setdefault(job.doc_version_id, job)
    return latest


@router.get("/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> DocumentOut:
    doc = db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail={"error_code": "E6004", "message": "文档不存在"})
    kb = db.get(KnowledgeBase, doc.kb_id)
    # 越权访问统一"不存在/无权限"，不泄露资源存在性
    if kb is None or (user.role.code != "admin" and kb.biz_group != user.biz_group):
        raise HTTPException(status_code=403, detail={"error_code": "E6003", "message": "没有该文档的访问权限"})
    meta = doc.meta
    version = db.get(DocVersion, doc.current_version_id) if doc.current_version_id else None
    return DocumentOut(
        id=doc.id, kb_id=doc.kb_id, doc_name=doc.doc_name, status=doc.status,
        current_version_id=doc.current_version_id,
        doc_type=meta.doc_type if meta else None,
        languages=meta.languages if meta else None,
        authority_level=meta.authority_level if meta else None,
        biz_group=meta.biz_group if meta else None,
        page_count=version.page_count if version else None,
        parse_status=version.parse_status if version else None,
    )
