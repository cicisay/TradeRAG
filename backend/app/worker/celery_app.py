"""Celery 应用（M6 步骤10-2）。

生产形态：API 进程只负责入队，独立的 worker 进程消费队列。
worker 启动：
    celery -A app.worker.celery_app:celery_app worker -Q ingest -l info -c 2
Windows 本机（无 fork）：
    python scripts/run_worker.py            # 内部固定 -P solo

关键配置的解释（每一条都对应一类真实故障）：
- task_acks_late=True：任务执行完才 ack。worker 被 kill 时消息会回到队列重投，
  而不是"消息已消费、任务没做完、数据永远缺一块"。
- worker_prefetch_multiplier=1：一次只预取 1 个任务。默认预取 4 个会让
  "一个大 OCR 任务卡住，后面 3 个小任务干等"。
- task_reject_on_worker_lost=True：进程消失时把任务退回队列（配合 acks_late）。
- soft/hard time limit：卡死的任务必须有上限，否则队列会被一个坏文件永久堵住。
- worker_max_tasks_per_child=50：定期回收子进程，防止 OCR/模型的内存碎片越跑越大。
- broker_connection_retry_on_startup=True：worker 比 Redis 先起来时不至于直接退出。
"""
import os

from celery import Celery

from app.config import get_settings

settings = get_settings()

# 刻意不配 result backend：
# ① 任务进度/结果以 ingest_job 台账为准（跨进程、可查询、可审计），Celery 的
#    result backend 只是重复存储，还要多订阅一个 pubsub 通道；
# ② 少一个 Redis 连接就少一类兼容问题（Redis 5 不支持 RESP3 的 HELLO 握手，
#    redis backend 会因此直接报错）。
celery_app = Celery(
    "traderag",
    broker=settings.redis_url,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=50,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": settings.ingest_task_time_limit + 600},
    task_default_queue=settings.celery_queue,
    task_soft_time_limit=settings.ingest_task_soft_time_limit,
    task_time_limit=settings.ingest_task_time_limit,
    # Windows 没有 fork：只能用 solo（单进程串行）或 threads。
    # 本机演示用 solo；生产 Linux 用 prefork 多并发。
    worker_pool=os.getenv("CELERY_POOL", "solo" if os.name == "nt" else "prefork"),
    worker_concurrency=int(os.getenv("CELERY_CONCURRENCY", "1")),
    task_routes={"ingest.document": {"queue": settings.celery_queue}},
)
