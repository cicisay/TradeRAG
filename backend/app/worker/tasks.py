"""Celery 任务定义（M6 步骤10-2）。

任务本身很薄：真正的逻辑都在 app/services/ingest.run_ingest_job 里。
这样同一份代码可以被三种执行方式复用（Celery / 线程 / 同步），
测试只需要覆盖 run_ingest_job，不用为了测任务去起 broker。

重试策略：
- run_ingest_job 会在失败时把 job 置回 queued 并返回 retry=True；
- 这里用 self.retry(countdown=退避) 让 broker 稍后重投；
- 最大次数由数据库的 attempts/max_attempts 控制（不用 Celery 的 max_retries，
  避免"两套计数"对不上导致实际重试次数翻倍）。
"""
import logging

from celery.exceptions import SoftTimeLimitExceeded

from app.services.ingest import run_ingest_job
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="ingest.document", max_retries=None)
def ingest_document(self, job_id: int) -> dict:
    """异步摄入一个文档版本：解析（含 OCR）→ 切分 → 嵌入 → 入库。"""
    try:
        result = run_ingest_job(job_id)
    except SoftTimeLimitExceeded:
        logger.error("任务超过软超时被中断 job=%s", job_id)
        from sqlalchemy import update

        from app.db.session import SessionLocal
        from app.models.jobs import STAGE_FAILED, IngestJob
        from datetime import datetime, timezone

        with SessionLocal() as s:
            s.execute(
                update(IngestJob).where(IngestJob.id == job_id).values(
                    stage=STAGE_FAILED,
                    message="处理超时（超过 %s 秒）" % celery_app.conf.task_soft_time_limit,
                    error="SoftTimeLimitExceeded",
                    finished_at=datetime.now(timezone.utc),
                )
            )
            s.commit()
        return {"job_id": job_id, "status": "failed", "error": "timeout"}

    if result.get("retry"):
        attempt = int(result.get("attempts") or 1)
        countdown = min(300, 10 * (2 ** max(0, attempt - 1)))
        logger.warning("任务第 %s 次失败，%s 秒后重试 job=%s", attempt, countdown, job_id)
        raise self.retry(countdown=countdown)

    return result
