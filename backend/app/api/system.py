"""系统能力探针（运维/演示用）：一眼看清"当前到底在用什么模型、什么向量库"。

为什么需要它：RAG 系统的效果高度依赖"真实模型是否生效"。如果悄悄降级成哈希嵌入，
检索质量会明显变差，但接口照样返回 200——这种"静默降级"最难排查。
本接口把实际生效的实现暴露出来（FR-702 可观测性）。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.deps import get_current_user, require_permission
from app.db.session import db_info, get_db
from app.models.user import User
from app.services.embedding import embedder_state, get_embedder
from app.services.llm import get_llm_client
from app.services.ocr import get_ocr_engine, ocr_state
from app.services.reranker import get_reranker, reranker_state
from app.services.vector_store import get_vector_store

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/capabilities")
def capabilities(
    probe: bool = Query(False, description="true 时真正加载模型并自检（首次较慢）"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    if probe:
        get_embedder()
        get_reranker()
        get_ocr_engine()

    llm = get_llm_client(db)
    store = get_vector_store()
    from app.services import vector_store as vs

    return {
        "env": settings.app_env,
        "embedder": embedder_state(),
        "reranker": reranker_state(),
        "ocr": ocr_state(),
        "vector_store": {
            "backend": store.name,
            "collection": settings.milvus_collection,
            # 期望 milvus 却降级到内存时这里非空：语义检索已失效，必须告警
            "degraded": vs.degraded_reason,
        },
        "llm": {"provider": llm.provider, "configured": llm.available, "model": llm.primary_model},
        "fallback_warning": embedder_state().get("active") == "hash-fallback",
    }


@router.post("/reindex")
def reindex(
    background: bool = True,
    user: User = Depends(require_permission("kb:manage")),
    db: Session = Depends(get_db),
) -> dict:
    """重建已解析文档的索引（切分 → 嵌入 → 入库）。

    什么时候需要：① 内存向量库重启后数据清空；② 换了嵌入模型（hash → bge-m3）；
    ③ 切分参数调整后需要让存量文档重新分段。

    background=true（默认）：每篇文档建一个后台任务，立刻返回，前端在任务面板看进度。
      文档多时这是唯一合理的方式——同步重建 200 篇会直接把请求挂死。
    background=false：同步执行，只为排障（能看到每一篇的异常）。
    """
    from app.models.document import DocVersion, STATUS_PARSED, Document
    from app.services.ingest import enqueue_ingest
    from app.services.pipeline import reindex_all

    if not background:
        result = reindex_all(db)
        result["embedder"] = embedder_state()
        return result

    docs = db.query(Document).filter(Document.status == STATUS_PARSED).all()
    jobs, failed = [], []
    for doc in docs:
        version = db.get(DocVersion, doc.current_version_id) if doc.current_version_id else None
        if version is None:
            failed.append({"doc_id": doc.id, "doc_name": doc.doc_name, "error": "缺少版本记录"})
            continue
        try:
            job, _ = enqueue_ingest(
                db, doc, version, created_by=user.username, force=True
            )
            jobs.append({"job_id": job.id, "doc_id": doc.id, "doc_name": doc.doc_name, "stage": job.stage})
        except Exception as exc:  # 单篇入队失败不阻断其他文档
            db.rollback()
            failed.append({"doc_id": doc.id, "doc_name": doc.doc_name, "error": str(exc)[:200]})
    return {
        "background": True,
        "total": len(docs),
        "queued": len(jobs),
        "failed": failed,
        "jobs": jobs,
        "embedder": embedder_state(),
        "hint": "进度见 /api/v1/jobs?active=true 或知识库页「处理任务」面板",
    }


@router.get("/db")
def database_status(user: User = Depends(require_permission("audit:read"))) -> dict:
    """数据库状态（M6）：后端类型、版本、时区口径、连接池占用、Alembic 版本。

    切 PostgreSQL 后最常问的三个问题都能在这里回答：
    1) 现在连的到底是 SQLite 还是 PG？（backend / url）
    2) 时区是不是统一 UTC？（server_timezone）
    3) 连接池有没有被打满？（pool.checked_out 接近 size+overflow 就是打满了）
    """
    return db_info()
