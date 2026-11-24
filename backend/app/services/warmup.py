"""模型预热：服务启动后在后台线程加载嵌入/重排/OCR，避免首个用户请求卡住。

为什么需要：
- bge-m3 有 2.2GB，首次加载要十几秒到几十秒；如果放在请求里，第一个上传/提问的用户
  会以为系统卡死（HTTP 超时）。
- 用后台线程加载，启动日志立刻可访问健康检查，模型就绪后请求恢复正常速度。
"""
import logging
import threading

from app.config import get_settings

logger = logging.getLogger(__name__)


def warmup_models_async() -> None:
    settings = get_settings()
    if settings.app_env == "test":
        return  # 测试环境不加载真实模型（用例用 hash 嵌入）
    if settings.embed_provider == "hash" and not settings.ocr_enabled:
        return
    threading.Thread(target=_load, name="model-warmup", daemon=True).start()


def _load() -> None:
    settings = get_settings()
    logger.info("模型预热开始（后台线程）…")
    try:
        if settings.embed_provider != "hash":
            from app.services.embedding import get_embedder

            emb = get_embedder()
            logger.info("嵌入器预热完成：%s（%s 维）", emb.name, emb.dim)
    except Exception:
        logger.exception("嵌入器预热失败，将在首次请求时重试")

    # 内存向量库不跨进程持久化：服务重启后为空，这里自动重建一次，
    # 否则用户会遇到"文档都在，但语义检索零召回"的诡异现象（BM25 仍能命中）。
    try:
        if settings.vector_store == "memory":
            from app.services.vector_store import get_vector_store

            store = get_vector_store()
            if store.count(settings.milvus_collection) == 0:
                from app.db.session import SessionLocal
                from app.services.pipeline import reindex_all

                db = SessionLocal()
                try:
                    result = reindex_all(db)
                    logger.info("内存向量库为空，已自动重建索引：%s", result)
                finally:
                    db.close()
    except Exception:
        logger.exception("自动重建索引失败，可手动调用 POST /api/v1/system/reindex")

    try:
        if settings.rerank_enabled:
            from app.services.reranker import get_reranker

            reranker = get_reranker()
            if reranker is not None:
                logger.info("重排器预热完成：%s", reranker.name)
    except Exception:
        logger.exception("重排器预热失败（降级启发式重排）")

    try:
        if settings.ocr_enabled:
            from app.services.ocr import get_ocr_engine

            engine = get_ocr_engine()
            logger.info("OCR 预热完成：%s", engine.name if engine else "不可用")
    except Exception:
        logger.exception("OCR 预热失败")
