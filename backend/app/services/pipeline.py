"""离线索引流水线：解析文本 → 切分 → 嵌入 → 写 chunk 表 + 向量库。

对应 PRD 5.1.4 离线流程（后续步骤接入 Celery 异步与 OCR 阶段）：
  上传 → 解析 → 【切分 → 嵌入 → 入库】← 本模块 → 完成/失败
- pipeline_trace_id 贯穿记录，便于 FR-803 badcase 快照回溯离线链路。
- 向量库记录字段严格对齐 PRD 6.2（chunk_id/chunk_type/language/doc_id/doc_version_id/
  kb_id/biz_group/sensitivity/doc_status/authority_level/content_hash）。
"""
import logging
import uuid

from sqlalchemy.orm import Session

from app.models.chunk import CHUNK_STATUS_ACTIVE, Chunk
from app.models.document import DocVersion, Document, DocumentMeta, STATUS_PARSED
from app.services import tracing
from app.services.chunker import split_text
from app.services.embedding import get_embedder
from app.services.parser import page_spans
from app.services.vector_store import VectorRecord, get_vector_store

logger = logging.getLogger(__name__)


class JobCanceled(Exception):
    """任务被人工取消：向上抛出，由调用方把任务置为 canceled 并回滚未提交的 chunk。"""


def run_indexing(
    doc_id: int,
    db: Session,
    progress=None,
    should_cancel=None,
) -> dict:
    """对已解析完成的文档执行切分+嵌入+入库（带 Langfuse 全链路追踪，11-1）。

    真正的实现见 _run_indexing_impl；本包装只负责开一条 ingest trace——
    单线程同步流程，用上下文管理器最省事，体内各阶段耗时由 impl 自行上报。
    """
    with tracing.trace(
        "ingest", trace_id=None, input={"doc_id": doc_id},
    ) as lf:
        result = _run_indexing_impl(doc_id, db, progress=progress, should_cancel=should_cancel)
        lf.update(output=result)
        return result


def _run_indexing_impl(
    doc_id: int,
    db: Session,
    progress=None,
    should_cancel=None,
) -> dict:
    """对已解析完成的文档执行切分+嵌入+入库。返回摘要统计。

    幂等性：重复执行会先删除该版本的旧 chunk 与向量（删旧写新，PRD 6.3）。

    progress(done_pct, message)：进度回调，供异步任务把百分比写进 ingest_job。
      回调必须自己吞掉异常——进度写失败不能把索引搞挂（PRD：可观测不能影响主链路）。
    should_cancel()：返回 True 则在批次边界中止（取消要"尽快"而不是"立刻"，
      不强杀线程，避免留下半个库的脏数据）。
    """
    version = db.query(DocVersion).filter(DocVersion.doc_id == doc_id).first()
    doc = db.get(Document, doc_id)
    meta = db.query(DocumentMeta).filter(DocumentMeta.doc_id == doc_id).first()
    if version is None or doc is None:
        raise ValueError(f"文档不存在或无版本记录 doc_id={doc_id}")
    if not version.raw_text or version.parse_status != "done":
        raise ValueError(f"文档尚未成功解析 doc_id={doc_id} parse_status={version.parse_status}")

    trace_id = uuid.uuid4().hex[:16]

    # 1) 删旧（幂等重跑）
    old_chunks = db.query(Chunk).filter(Chunk.doc_version_id == version.id).all()
    if old_chunks:
        store = get_vector_store()
        collection = _collection_name()
        store.ensure_collection(collection, _embedder_dim())
        store.delete_by_doc_version(collection, version.id)
        for c in old_chunks:
            db.delete(c)
        db.flush()

    def report(pct: int, msg: str) -> None:
        if progress is None:
            return
        try:
            progress(int(pct), msg)
        except Exception:  # 进度上报失败不影响索引
            logger.debug("进度回调失败 pct=%s msg=%s", pct, msg, exc_info=True)

    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise JobCanceled(f"文档 {doc_id} 的索引任务已取消")

    report(8, "切分中")
    # 2) 切分（带页码定位：PDF 用换页符还原每块来自第几页）
    spans = page_spans(version.raw_text, version.page_count)
    pieces = split_text(version.raw_text, page_spans=spans)
    if not pieces:
        version.parse_status = "failed"
        version.parse_error = "切分结果为空"
        db.commit()
        return {"doc_id": doc_id, "trace_id": trace_id, "chunks": 0, "indexed": 0}

    check_cancel()
    # 3) 嵌入（分批 + 进度上报）：一次把几百块全塞进模型会长时间"没动静"，
    #    分批既能让前端看到进度，也方便在两批之间响应取消。
    embedder = get_embedder()
    texts = [p["content"] for p in pieces]
    total = len(texts)
    # 大约切成 10 个进度台阶，且单批不少于 8 条（批太小会让模型调用次数暴增）
    batch_size = max(8, min(64, (total + 9) // 10 or 8))
    vectors: list[list[float]] = []
    for start in range(0, total, batch_size):
        chunk_texts = texts[start:start + batch_size]
        vectors.extend(embedder.embed(chunk_texts))
        done = min(total, start + len(chunk_texts))
        report(10 + int(80 * done / max(1, total)), f"向量化 {done}/{total}")
        check_cancel()

    # 4) 写 chunk 表
    chunk_rows = [
        Chunk(
            doc_version_id=version.id,
            chunk_index=p["chunk_index"],
            content=p["content"],
            language=p["language"],
            page_no=p.get("page_no"),
            # OCR 置信度：整篇平均值（逐页精确值需扩展表结构，当前按文档级标记低置信度页码）
            ocr_confidence=version.ocr_confidence if version.ocr_used else None,
            status=CHUNK_STATUS_ACTIVE,
        )
        for p in pieces
    ]
    db.add_all(chunk_rows)
    db.flush()  # 拿到 chunk.id

    # 5) 写向量库（标量字段对齐 PRD 6.2，检索期强制过滤的数据基础）
    store = get_vector_store()
    collection = _collection_name()
    store.ensure_collection(collection, embedder.dim)
    records = [
        VectorRecord(
            vector=vec,
            chunk_id=row.id,
            chunk_type="content",
            language=row.language,
            doc_id=doc.id,
            doc_version_id=version.id,
            kb_id=doc.kb_id,
            biz_group=meta.biz_group if meta else "default",
            sensitivity=meta.sensitivity if meta else 0,
            doc_status="active",
            authority_level=meta.authority_level if meta else 2,
            content_hash=f"{doc.id}-{version.version_no}-{row.chunk_index}",
        )
        for row, vec in zip(chunk_rows, vectors)
    ]
    indexed = store.insert(collection, records)

    # 6) 收尾
    report(95, "写入索引")
    version.pipeline_trace_id = trace_id
    doc.status = STATUS_PARSED
    db.commit()
    report(100, "完成")
    logger.info(
        "索引完成 doc_id=%s trace=%s chunks=%s vectors=%s embed=%s store=%s",
        doc_id, trace_id, len(chunk_rows), indexed, embedder.name, store.name,
    )
    return {
        "doc_id": doc_id, "trace_id": trace_id,
        "chunks": len(chunk_rows), "indexed": indexed,
        "embedder": embedder.name, "store": store.name,
    }


def _collection_name() -> str:
    from app.config import get_settings
    from app.services.vector_store import sanitize_collection_name

    return sanitize_collection_name(get_settings().milvus_collection)


def reindex_all(db: Session) -> dict:
    """重建全部已解析文档的索引（切分 → 嵌入 → 入库）。

    什么时候需要：① 内存向量库重启后数据清空；② 换了嵌入模型（hash → bge-m3）。
    注意必须在**使用向量库的进程内**执行——内存向量库不跨进程共享。
    """
    docs = db.query(Document).filter(Document.status == STATUS_PARSED).all()
    ok, failed, chunks = 0, [], 0
    for doc in docs:
        try:
            stats = run_indexing(doc.id, db)
            ok += 1
            chunks += stats["chunks"]
        except Exception as exc:  # 单篇失败不阻断整体重建
            db.rollback()
            logger.exception("重建索引失败 doc_id=%s", doc.id)
            failed.append({"doc_id": doc.id, "doc_name": doc.doc_name, "error": str(exc)[:200]})
    return {"total": len(docs), "succeeded": ok, "failed": failed, "chunks": chunks}


def _embedder_dim() -> int:
    return get_embedder().dim
