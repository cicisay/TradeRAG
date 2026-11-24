"""Celery + Redis 真实链路冒烟（M6 步骤10-2）。

与 pytest 的区别：pytest 走 INGEST_MODE=inline（不起 broker），这里是真的
「API 进程 → Redis 队列 → 独立 worker 进程 → SQLite 台账」。

它会做这些事：
1. 用独立的 queue_smoke.db（不污染开发库），起 uvicorn 在 8020；
2. 起一个 Celery worker（Windows 下 solo 池）；
3. 登录 → 上传一个多段文本 → 拿 job_id；
4. 每 0.4 秒轮询一次任务状态，记录完整的 (stage, progress) 轨迹；
5. 断言：stage=indexed、progress=100、chunk 数 > 0、attempts=1；
6. 收尾杀进程，报告写 data/queue_smoke_report.json。

前置：Redis 已启动（scripts/local_redis.py up，或 docker run redis）。
用法：python scripts/queue_smoke.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent.parent
PY = sys.executable
DB = BASE / "data" / "queue_smoke.db"
PORT = int(os.getenv("SMOKE_PORT", "8020"))
REDIS = os.getenv("SMOKE_REDIS_URL", os.getenv("REDIS_URL", "redis://127.0.0.1:56379/0"))
API_LOG = BASE / "data" / "queue_smoke_api.log"
WORKER_LOG = BASE / "data" / "queue_smoke_worker.log"
REPORT = BASE / "data" / "queue_smoke_report.json"

SMOKE_ENV = {
    **os.environ,
    "DATABASE_URL": f"sqlite:///{DB}",
    "INGEST_MODE": "celery",          # 强制走队列（即使向量库是内存版）
    "REDIS_URL": REDIS,
    "APP_ENV": "smoke",
    "EMBED_PROVIDER": "hash",          # 冒烟只验证链路，不加载 2GB 模型
    "RERANK_PROVIDER": "heuristic",
    "OCR_ENABLED": "false",
    "LOGIN_RATE_LIMIT": "0",
}

CORPUS = "\n\n".join(
    f"第 {i} 节 报关单证与贸易术语说明。商业发票需与装箱单一致，提单收货人须与合同一致。"
    f"FOB 条款下卖方负责装船前费用，CIF 条款下卖方另需投保。"
    for i in range(1, 41)
)


def log(msg: str) -> None:
    print(msg, flush=True)


def wait_redis(timeout: float = 15.0) -> bool:
    import redis

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # protocol=2：Redis 5 不支持 redis-py 8 默认的 RESP3 握手（HELLO 3）
            client = redis.Redis.from_url(REDIS, socket_connect_timeout=0.5,
                                          socket_timeout=0.5, protocol=2)
            if client.ping():
                return True
        except Exception:
            time.sleep(0.5)
    return False


def wait_api(client: httpx.Client, timeout: float = 180.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get("/health").status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main() -> int:
    result: dict = {"ok": False, "timeline": [], "errors": []}

    if not wait_redis():
        log(f"[X] Redis 连不上：{REDIS}")
        log("    先启动：python scripts/local_redis.py up")
        return 2

    if DB.exists():
        DB.unlink()
    for p in (API_LOG, WORKER_LOG):
        p.write_text("", encoding="utf-8")

    api_proc = worker_proc = None
    try:
        log("[1/5] 启动 API（8020）…")
        with open(API_LOG, "w", encoding="utf-8") as f:
            api_proc = subprocess.Popen(
                [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
                cwd=BASE, env=SMOKE_ENV, stdout=f, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )

        log("[2/5] 启动 Celery worker …")
        with open(WORKER_LOG, "w", encoding="utf-8") as f:
            worker_proc = subprocess.Popen(
                [PY, "scripts/run_worker.py", "-l", "INFO", "-c", "1"],
                cwd=BASE, env=SMOKE_ENV, stdout=f, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )

        with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=60) as client:
            if not wait_api(client):
                result["errors"].append("API 未在 180 秒内就绪，见 data/queue_smoke_api.log")
                return 1

            log("[3/5] 登录并上传 …")
            r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
            r.raise_for_status()
            headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

            kbs = client.get("/api/v1/knowledge-bases", headers=headers).json()
            kb_id = kbs[0]["id"] if kbs else client.post(
                "/api/v1/knowledge-bases", headers=headers,
                json={"name": "冒烟库", "biz_group": "default", "product_line": "general"},
            ).json()["id"]

            t0 = time.perf_counter()
            up = client.post(
                "/api/v1/documents/upload",
                headers=headers,
                data={"kb_id": str(kb_id), "doc_type": "policy"},
                files={"file": ("queue-smoke.txt", CORPUS.encode("utf-8"), "text/plain")},
            )
            accepted_ms = int((time.perf_counter() - t0) * 1000)
            result["upload_status"] = up.status_code
            result["upload_latency_ms"] = accepted_ms
            if up.status_code not in (200, 202):
                result["errors"].append(f"上传失败 {up.status_code}: {up.text[:300]}")
                return 1
            body = up.json()
            job_id = body["job_id"]
            log(f"    上传返回 {up.status_code} / {accepted_ms}ms，job={job_id} mode={body.get('queue_mode')}")

            log("[4/5] 轮询任务进度 …")
            last = None
            deadline = time.time() + 180
            job = {}
            while time.time() < deadline:
                job = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
                key = (job.get("stage"), job.get("progress"))
                if key != last:
                    result["timeline"].append({
                        "t_ms": int((time.perf_counter() - t0) * 1000),
                        "stage": job.get("stage"), "progress": job.get("progress"),
                        "message": job.get("message"),
                    })
                    log(f"    {key[0]:<10} {key[1]:>3}%  {job.get('message') or ''}")
                    last = key
                if job.get("terminal"):
                    break
                time.sleep(0.4)

            log("[5/5] 校验结果 …")
            checks = {
                "任务完成": job.get("stage") == "indexed",
                "进度到 100": job.get("progress") == 100,
                "分片数 > 0": bool((job.get("result") or {}).get("chunks")),
                "只尝试 1 次": job.get("attempts") == 1,
                "broker 是 celery": job.get("broker") == "celery",
                "有 celery task id": bool(job.get("task_id")),
                "上传未阻塞": accepted_ms < 5000,
            }
            for name, ok in checks.items():
                log(f"    {'OK ' if ok else 'ERR'} {name}")
                if not ok:
                    result["errors"].append(name)
            result["job"] = job
            result["checks"] = checks
            result["ok"] = all(checks.values())

            stats = client.get("/api/v1/jobs/stats", headers=headers).json()
            result["stats"] = {
                "mode": stats.get("mode"),
                "broker_available": stats.get("broker", {}).get("available"),
                "queued_in_broker": stats.get("broker", {}).get("queued_in_broker"),
                "jobs": stats.get("jobs"),
            }
            log(f"    队列概览：{json.dumps(result['stats'], ensure_ascii=False)}")
            return 0 if result["ok"] else 1
    finally:
        for proc, name in ((worker_proc, "worker"), (api_proc, "api")):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                log(f"    已停止 {name}")
        REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"报告：{REPORT}")


if __name__ == "__main__":
    sys.exit(main())
