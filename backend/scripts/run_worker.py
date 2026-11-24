"""启动 Celery worker（M6 步骤10-2）。

用法：
    python scripts/run_worker.py                 # 默认 1 并发
    python scripts/run_worker.py --concurrency 4 # 生产（Linux）可多并发

为什么不用 `celery -A ... worker` 直接跑：
Windows 没有 fork，Celery 默认的 prefork 池会直接报错。这里自动切 --pool=solo
（单进程串行），并在 Linux 上保留 prefork。生产建议用 systemd/supervisor
起多个 solo worker，或直接在 Linux 用 prefork + 多副本。
"""
import argparse
import logging
import os
import sys
from pathlib import Path

# 直接 `python scripts/run_worker.py` 时，sys.path[0] 是 scripts/，
# 找不到 app 包。把项目根插到最前面（celery 子进程也会继承这个 cwd）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="启动异步摄入 worker")
    parser.add_argument("-c", "--concurrency", type=int, default=int(os.getenv("CELERY_CONCURRENCY", "1")))
    parser.add_argument("-Q", "--queue", default=os.getenv("CELERY_QUEUE", get_settings().celery_queue))
    parser.add_argument("-l", "--loglevel", default="INFO")
    args, _ = parser.parse_known_args()

    argv = ["worker", f"--loglevel={args.loglevel}", "-Q", args.queue,
            f"--concurrency={args.concurrency}"]
    if os.name == "nt":
        argv += ["--pool=solo"]

    # 启动前自检：broker 连不上时给出人能看懂的提示，而不是一堆 kombu 堆栈
    from app.services.ingest import broker_available, resolve_mode

    mode = resolve_mode()
    if mode != "celery":
        logger.warning(
            "当前 ingest_mode=%s（vector_store=%s）。worker 起来也会空转；"
            "要验证 Celery 链路请设置 INGEST_MODE=celery 且 VECTOR_STORE=milvus（或先跑 queue_smoke.py）。",
            mode, get_settings().vector_store,
        )
    if not broker_available(ttl=0):
        logger.error("Redis 不可连：%s（先启动 Redis 或改 REDIS_URL）", get_settings().redis_url)
        return 2

    logger.info("worker 启动：queue=%s concurrency=%s broker=%s",
                args.queue, args.concurrency, get_settings().redis_url)
    celery_app.worker_main(argv)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    sys.exit(main())
