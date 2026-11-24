"""异步摄入服务：入队、执行、进度、重试、取消（M6 步骤10-2）。

给新手的说明（为什么需要这一层）：
上传 100MB 扫描件 PDF 时，OCR 一页要 0.3~1 秒，几百页就是几分钟。若在上传接口里同步做：
  ① 浏览器一直转圈，Nginx 默认 60s 直接 504；
  ② 用户以为没成功，再点一次 → 同一文件被索引两遍；
  ③ 进程重启，任务直接消失且没人知道做到哪了。
解决方式是把"重活"变成有台账的后台任务：接口只登记 + 入队（毫秒级返回），
worker 真正干活，每完成一个阶段就更新 ingest_job 的 stage/progress，前端轮询显示。

三种执行模式（settings.ingest_mode）：
- celery：Celery + Redis，真正的跨进程队列，可多 worker 横向扩容（生产形态，需 Milvus）。
- thread：同进程后台线程。**本机内存向量库只能这样** —— 内存向量库是进程私有变量，
  worker 进程里写进去的向量，API 进程读不到（表现为"任务成功但搜不到"）。
- inline：请求内同步执行，只为测试与排障（能拿到完整堆栈）。

可靠性要点：
- 抢占用条件 UPDATE（CAS），两个 worker 同时拿同一任务只有一个能成功；
- 进度用独立短会话写入，主事务回滚不会把"做到 60%"的痕迹一起抹掉；
- 取消是协作式的：只在批次边界检查，不强杀线程（强杀会留下半截 chunk）；
- worker 崩溃：stage 停在 parsing/embedding 且 updated_at 超过 ingest_stale_seconds，
  其他 worker 可以接管重跑（索引本身幂等：删旧写新）。
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, update
from sqlalchemy.orm import Session

from app.config import BASE_DIR, get_settings
from app.db.session import SessionLocal
from app.models.document import (
    PARSE_DONE, PARSE_FAILED, STATUS_FAILED, STATUS_PARSED, STATUS_PARSING,
    DocVersion, Document,
)
from app.models.jobs import (
    PIPELINE_VERSION, STAGE_CANCELED, STAGE_CHUNKING, STAGE_EMBEDDING, STAGE_FAILED,
    STAGE_INDEXED, STAGE_NAMES, STAGE_PARSING, STAGE_QUEUED, STAGE_RUNNING,
    STAGE_TERMINAL, IngestJob,
)
from app.services.parser import parse_file
from app.services.pipeline import JobCanceled, run_indexing

logger = logging.getLogger(__name__)

# broker 探活缓存：(时间戳, 是否可用)。不能每个请求都 ping 一次 Redis。
_probe: dict = {"at": 0.0, "ok": False, "detail": ""}


class IngestError(Exception):
    """不可重试的业务错误（文件缺失、解析为空等）：直接置 failed，不浪费重试次数。"""


# ---------------------------------------------------------------- 模式与探活
def redis_client():
    """建 Redis 连接（统一 protocol=2）。

    为什么强制 RESP2：redis-py 8.x 默认先发 `HELLO 3` 切 RESP3，
    而 Redis 6 以下（含 Windows 移植版 5.0）没有这个命令，会直接报
    `unknown command HELLO`。生产 Redis 7 用 RESP2 也完全够用，
    统一成 RESP2 可以让本地与生产行为一致。
    """
    import redis

    return redis.Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
        protocol=2,
    )


def broker_available(ttl: float = 5.0) -> bool:
    """Redis 是否可连。带 5 秒缓存，避免每请求都探测（探测本身也要建连接）。"""
    if get_settings().ingest_mode in ("inline", "thread"):
        return False
    now = time.time()
    if now - _probe["at"] < ttl:
        return bool(_probe["ok"])
    ok, detail = False, ""
    try:
        client = redis_client()
        ok = bool(client.ping())
        client.close()
    except Exception as exc:  # 没装 redis / 没启动 / 密码错，都归为"不可用"
        detail = str(exc)[:160]
    _probe.update(at=now, ok=ok, detail=detail)
    return ok


def resolve_mode() -> str:
    """决定用哪种执行方式。auto 的核心判断：内存向量库不能跨进程。"""
    settings = get_settings()
    mode = (settings.ingest_mode or "auto").lower()
    if mode in ("inline", "thread", "celery"):
        return mode
    if settings.vector_store == "memory":
        return "thread"
    return "celery" if broker_available() else "thread"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 台账操作
def _progress(job_id: int, stage: str, pct: int, message: str | None = None) -> None:
    """独立短会话写进度：主事务回滚时，进度痕迹仍然保留。"""
    try:
        with SessionLocal() as s:
            s.execute(
                update(IngestJob)
                .where(IngestJob.id == job_id, IngestJob.stage != STAGE_CANCELED)
                .values(
                    stage=stage,
                    progress=max(0, min(100, int(pct))),
                    message=(message or "")[:250] or None,
                    updated_at=_now(),
                )
            )
            s.commit()
    except Exception:  # 进度写失败绝不能影响索引主流程
        logger.debug("写入任务进度失败 job=%s stage=%s", job_id, stage, exc_info=True)


def is_canceled(job_id: int) -> bool:
    """协作式取消检查：每批嵌入前调用一次，用独立会话读到最新状态。"""
    try:
        with SessionLocal() as s:
            stage = s.query(IngestJob.stage).filter(IngestJob.id == job_id).scalar()
        return stage == STAGE_CANCELED
    except Exception:
        return False


def _claim(db: Session, job_id: int) -> IngestJob | None:
    """抢占任务（CAS）：只有一个 worker 能把 queued/超时的 running 改成 parsing。"""
    settings = get_settings()
    cutoff = _now() - timedelta(seconds=settings.ingest_stale_seconds)
    claimable = or_(
        IngestJob.stage.in_((STAGE_QUEUED, STAGE_FAILED)),
        and_(IngestJob.stage.in_(STAGE_RUNNING), IngestJob.updated_at < cutoff),
    )
    res = db.execute(
        update(IngestJob)
        .where(IngestJob.id == job_id, claimable)
        .values(
            stage=STAGE_PARSING,
            attempts=IngestJob.attempts + 1,
            started_at=func.coalesce(IngestJob.started_at, _now()),
            finished_at=None,
            error=None,
            message="开始处理",
            updated_at=_now(),
        )
    )
    db.commit()
    if not res.rowcount:
        return None
    return db.get(IngestJob, job_id)


def job_to_dict(job: IngestJob, doc_name: str | None = None) -> dict:
    return {
        "id": job.id,
        "doc_id": job.doc_id,
        "doc_version_id": job.doc_version_id,
        "doc_name": doc_name,
        "stage": job.stage,
        "stage_name": STAGE_NAMES.get(job.stage, job.stage),
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "task_id": job.task_id,
        "broker": job.broker,
        "request_id": job.request_id,
        "created_by": job.created_by,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "duration_ms": (
            int((job.finished_at - job.started_at).total_seconds() * 1000)
            if job.finished_at and job.started_at else None
        ),
        "result": json.loads(job.result_json) if job.result_json else None,
        "running": job.stage in STAGE_RUNNING,
        "terminal": job.stage in STAGE_TERMINAL,
    }


def get_job(db: Session, job_id: int) -> IngestJob | None:
    return db.get(IngestJob, job_id)


# ---------------------------------------------------------------- 入队
def enqueue_ingest(
    db: Session,
    doc: Document,
    version: DocVersion,
    *,
    created_by: str | None = None,
    request_id: str | None = None,
    force: bool = False,
) -> tuple[IngestJob, bool]:
    """登记并入队。返回 (job, 是否新建了记录)。

    幂等规则：同一 (版本, pipeline_version) 已有在途任务时直接复用它，
    不会因为用户连点五次上传而排五个任务、并发把同一份文档索引五遍。
    """
    job = (
        db.query(IngestJob)
        .filter(IngestJob.doc_version_id == version.id,
                IngestJob.pipeline_version == PIPELINE_VERSION)
        .first()
    )
    reused = job is not None

    if job is not None and job.stage in (STAGE_QUEUED, *STAGE_RUNNING) and not force:
        return job, reused

    if job is None:
        job = IngestJob(
            doc_id=doc.id, doc_version_id=version.id, pipeline_version=PIPELINE_VERSION,
            created_by=created_by, request_id=request_id,
            max_attempts=get_settings().ingest_max_attempts,
        )
        db.add(job)
    else:
        job.stage = STAGE_QUEUED
        job.progress = 0
        job.message = "重新排队"
        job.error = None
        job.attempts = 0
        job.finished_at = None
        job.request_id = request_id or job.request_id

    db.commit()
    db.refresh(job)

    mode = resolve_mode()
    if mode == "inline":
        job.broker = "inline"
        db.commit()
        run_ingest_job(job.id)  # 同步执行：测试/排障，能直接看到异常
        db.refresh(job)
        return job, not reused

    if mode == "celery":
        try:
            from app.worker.tasks import ingest_document  # 延迟导入：避免无 celery 环境时启动失败

            async_result = ingest_document.apply_async(args=[job.id], queue=get_settings().celery_queue)
            job.task_id = async_result.id
            job.broker = "celery"
            db.commit()
            logger.info("任务已入队 job=%s task=%s queue=%s", job.id, async_result.id,
                        get_settings().celery_queue)
            return job, not reused
        except Exception:
            # broker 挂了不能让上传失败：降级同进程线程，并在日志里留痕
            logger.exception("Celery 入队失败，降级为同进程线程 job=%s", job.id)
            mode = "thread"

    job.broker = "thread"
    db.commit()
    _start_thread(job.id, delay=0)
    logger.info("任务已交给后台线程 job=%s", job.id)
    return job, not reused


def _start_thread(job_id: int, delay: float = 0) -> None:
    """同进程线程执行 + 失败退避重试（线程模式没有 broker 帮忙重投）。"""

    def runner() -> None:
        if delay:
            time.sleep(delay)
        result = run_ingest_job(job_id)
        if result.get("retry"):
            attempt = int(result.get("attempts") or 1)
            _start_thread(job_id, delay=min(60, 3 * 2 ** (attempt - 1)))

    threading.Thread(target=runner, name=f"ingest-{job_id}", daemon=True).start()


# ---------------------------------------------------------------- 执行
def run_ingest_job(job_id: int) -> dict:
    """执行一个任务（Celery task / 线程 / 同步调用共用这一份实现）。"""
    db = SessionLocal()
    try:
        job = _claim(db, job_id)
        if job is None:
            return {"job_id": job_id, "status": "skipped", "reason": "已被其他 worker 领取或已结束"}
        attempts = job.attempts
        try:
            summary = _execute(db, job)
        except JobCanceled:
            db.rollback()
            _progress(job_id, STAGE_CANCELED, 0, "已取消")
            with SessionLocal() as s:
                s.execute(update(IngestJob).where(IngestJob.id == job_id)
                          .values(finished_at=_now(), updated_at=_now()))
                s.commit()
            logger.info("任务已取消 job=%s", job_id)
            return {"job_id": job_id, "status": "canceled"}
        except Exception as exc:
            db.rollback()
            logger.exception("任务执行失败 job=%s attempts=%s", job_id, attempts)
            return _handle_failure(db, job_id, exc, attempts)

        # 成功收尾
        with SessionLocal() as s:
            s.execute(
                update(IngestJob).where(IngestJob.id == job_id).values(
                    stage=STAGE_INDEXED, progress=100, message="完成",
                    error=None, finished_at=_now(), updated_at=_now(),
                    result_json=json.dumps(summary, ensure_ascii=False),
                )
            )
            s.commit()
        logger.info("任务完成 job=%s %s", job_id, summary)
        return {"job_id": job_id, "status": "indexed", "attempts": attempts, **summary}
    finally:
        db.close()


def _execute(db: Session, job: IngestJob) -> dict:
    """真正的流水线：解析（含 OCR）→ 切分 → 嵌入 → 入库，全程上报进度。"""
    doc = db.get(Document, job.doc_id)
    version = db.get(DocVersion, job.doc_version_id)
    if doc is None or version is None:
        raise IngestError("文档或版本记录不存在（可能已被删除）")

    # ---- 阶段1：解析 ----
    if version.parse_status != PARSE_DONE or not (version.raw_text or "").strip():
        _progress(job.id, STAGE_PARSING, 3, "读取原始文件")
        path = BASE_DIR / version.file_path
        if not path.exists():
            raise IngestError(f"原始文件缺失：{version.file_path}")
        data = path.read_bytes()
        _progress(job.id, STAGE_PARSING, 8, "解析文档（扫描件会做 OCR，较慢）")

        doc.status = STATUS_PARSING
        db.commit()
        result = parse_file(doc.doc_name, data)

        version.parse_status = PARSE_DONE if result.ok else PARSE_FAILED
        version.raw_text = result.text if result.ok else None
        version.page_count = result.page_count
        version.parse_error = result.error
        version.ocr_used = result.ocr_used
        version.ocr_confidence = result.ocr_confidence
        version.ocr_low_pages = result.low_confidence_pages
        version.ocr_error = result.ocr_error
        doc.status = STATUS_PARSED if result.ok else STATUS_FAILED
        db.commit()

        if not result.ok:
            # 解析不出文本 = 文件本身有问题，重试也没用 → 不重试
            raise IngestError(f"解析失败：{result.error or '未提取到文本内容'}")
        page_info = f"{result.page_count} 页" if result.page_count else "单页/无分页"
        ocr_info = f"，OCR 置信度 {result.ocr_confidence:.3f}" if result.ocr_used and result.ocr_confidence else ""
        _progress(job.id, STAGE_PARSING, 30, f"解析完成（{page_info}{ocr_info}）")
    else:
        _progress(job.id, STAGE_PARSING, 30, "复用已解析文本")

    # ---- 阶段2：切分 + 嵌入 + 入库（进度 30→95）----
    _progress(job.id, STAGE_CHUNKING, 32, "切分中")

    def on_progress(pct: int, msg: str) -> None:
        overall = 30 + int(max(0, min(100, pct)) * 0.65)
        stage = STAGE_CHUNKING if pct < 10 else (STAGE_EMBEDDING if pct < 95 else STAGE_INDEXED)
        _progress(job.id, stage, min(95, overall), msg)

    stats = run_indexing(
        doc.id, db, progress=on_progress, should_cancel=lambda: is_canceled(job.id)
    )
    if not stats.get("chunks"):
        raise IngestError("切分结果为空（文档可能只有空白内容）")
    return {
        "chunks": stats["chunks"],
        "indexed": stats.get("indexed", 0),
        "embedder": stats.get("embedder"),
        "store": stats.get("store"),
        "trace_id": stats.get("trace_id"),
    }


def _handle_failure(db: Session, job_id: int, exc: Exception, attempts: int) -> dict:
    """失败处理：可重试的回到 queued，不可重试或次数用尽置 failed。"""
    retryable = not isinstance(exc, IngestError)
    with SessionLocal() as s:
        job = s.get(IngestJob, job_id)
        if job is None:
            return {"job_id": job_id, "status": "failed", "error": str(exc)[:300]}
        max_attempts = job.max_attempts or get_settings().ingest_max_attempts
        will_retry = retryable and attempts < max_attempts
        if will_retry:
            job.stage = STAGE_QUEUED
            job.message = f"第 {attempts} 次失败，等待重试"
            job.error = str(exc)[:1000]
        else:
            job.stage = STAGE_FAILED
            job.message = "重试用尽" if retryable else "不可重试的错误"
            job.error = str(exc)[:1000]
            job.finished_at = _now()
            doc = s.get(Document, job.doc_id)
            if doc is not None:
                doc.status = STATUS_FAILED
        job.updated_at = _now()
        s.commit()
    return {
        "job_id": job_id, "status": "retry" if will_retry else "failed",
        "attempts": attempts, "retry": will_retry, "error": str(exc)[:300],
    }


def cancel_job(db: Session, job: IngestJob) -> dict:
    """取消任务：DB 置 canceled（协作式生效）+ 尽量撤销还在队列里的 Celery 消息。"""
    if job.stage in STAGE_TERMINAL:
        return {"canceled": False, "reason": "任务已结束，无法取消", "job": job_to_dict(job)}
    revoked = False
    if job.task_id and job.broker == "celery":
        try:
            from app.worker.celery_app import celery_app

            celery_app.control.revoke(job.task_id, terminate=False)
            revoked = True
        except Exception:
            logger.warning("撤销 Celery 任务失败 job=%s task=%s", job.id, job.task_id)
    job.stage = STAGE_CANCELED
    job.message = "已取消"
    job.finished_at = _now()
    db.commit()
    db.refresh(job)
    return {"canceled": True, "revoked": revoked, "job": job_to_dict(job)}


# ---------------------------------------------------------------- 队列概览
def queue_status(db: Session) -> dict:
    """给管理后台看的队列概览：不看 Celery 的实时状态，而以台账为准（更稳、跨进程一致）。"""
    settings = get_settings()
    counts = {stage: 0 for stage in STAGE_NAMES}
    for stage, n in db.query(IngestJob.stage, func.count(IngestJob.id)).group_by(IngestJob.stage):
        counts[stage] = int(n)

    day_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    indexed_today = (
        db.query(func.count(IngestJob.id))
        .filter(IngestJob.stage == STAGE_INDEXED, IngestJob.finished_at >= day_start)
        .scalar() or 0
    )

    recent = (
        db.query(IngestJob, Document.doc_name)
        .outerjoin(Document, Document.id == IngestJob.doc_id)
        .order_by(IngestJob.id.desc())
        .limit(20)
        .all()
    )
    durations = [
        int((j.finished_at - j.started_at).total_seconds() * 1000)
        for j, _ in recent
        if j.stage == STAGE_INDEXED and j.finished_at and j.started_at
    ]

    queued_in_broker = None
    if broker_available():
        try:
            client = redis_client()
            queued_in_broker = int(client.llen(settings.celery_queue))
            client.close()
        except Exception:
            queued_in_broker = None

    return {
        "mode": resolve_mode(),
        "configured_mode": settings.ingest_mode,
        "vector_store": settings.vector_store,
        "broker": {
            "type": "redis",
            "url": _mask_url(settings.redis_url),
            "available": broker_available(ttl=0),
            "queue": settings.celery_queue,
            "queued_in_broker": queued_in_broker,
        },
        "jobs": {
            **counts,
            "running": sum(counts[s] for s in STAGE_RUNNING),
            "pending": counts[STAGE_QUEUED],
            "indexed_today": int(indexed_today),
            "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None,
            "recent_avg_sample": len(durations),
        },
        "recent": [job_to_dict(j, name) for j, name in recent[:8]],
    }


def _mask_url(url: str) -> str:
    """脱敏：redis://:password@host:port/0 → redis://:***@host:port/0。"""
    if "@" not in url:
        return url
    head, tail = url.split("@", 1)
    if ":" in head.split("//")[-1]:
        scheme = head.split("//")[0]
        return f"{scheme}//:***@{tail}"
    return url


def requeue_stale(db: Session, older_than_seconds: int | None = None) -> int:
    """把"卡在进行中"的任务重新排队（worker 被杀 / 断电后的恢复）。

    启动时调用一次即可：只处理超过 stale 阈值的任务，避免误抢正在跑的任务。
    """
    threshold = older_than_seconds or get_settings().ingest_stale_seconds
    cutoff = _now() - timedelta(seconds=threshold)
    res = db.execute(
        update(IngestJob)
        .where(IngestJob.stage.in_(STAGE_RUNNING), IngestJob.updated_at < cutoff)
        .values(stage=STAGE_QUEUED, message="检测到中断，重新排队", updated_at=_now())
    )
    db.commit()
    if res.rowcount:
        logger.warning("发现 %s 个中断任务，已重新排队", res.rowcount)
    return int(res.rowcount or 0)
