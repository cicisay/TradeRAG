"""10-4 部署验收压测：多线程模拟并发用户，产出 P50/P95/P99 与错误率。

场景：
  read  —— 80 并发混合读（文档列表 60% + 管理员检索测试台 40%，测 RAG 检索链路吞吐）
  chat  —— 低并发 SSE 流式问答（测 LLM 链路在并发下的稳定性；外部 LLM 是瓶颈，不宜 80 并发硬打）

用法（在 backend/）：
    .venv/Scripts/python scripts/stress_test.py --base http://127.0.0.1:8080 \
        --scenario read --concurrency 80 --seconds 120
"""
import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def http_json(base, path, data=None, token=None, timeout=60):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=body, headers=headers)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def login(base, username, password):
    _, cap = http_json(base, "/api/v1/auth/captcha")
    st, tok = http_json(base, "/api/v1/auth/login", {
        "username": username, "password": password,
        "captcha_id": cap["captcha_id"], "captcha_text": cap["debug_answer"], "role": "admin",
    })
    assert st == 200, tok
    return tok["access_token"]


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.lat = []
        self.errors = 0
        self.count = 0
        self.err_kinds = {}

    def record(self, ms, ok=True, kind=""):
        with self.lock:
            if ok:
                self.lat.append(ms)
            else:
                self.errors += 1
                self.err_kinds[kind] = self.err_kinds.get(kind, 0) + 1
            self.count += 1


def worker_read(base, token, stop_at, stats):
    """读混合：文档列表（轻）+ 检索测试台（重，走嵌入+重排）。"""
    import random

    rng = random.Random(threading.get_ident())
    while time.time() < stop_at:
        t0 = time.perf_counter()
        try:
            if rng.random() < 0.4:
                http_json(base, "/api/v1/debug/retrieval",
                          {"query": "DAP 与 DDP 的区别", "top_k": 5}, token=token, timeout=60)
            else:
                http_json(base, "/api/v1/documents?page=1&page_size=10", token=token, timeout=30)
            stats.record((time.perf_counter() - t0) * 1000)
        except urllib.error.HTTPError as e:
            stats.record((time.perf_counter() - t0) * 1000, ok=False, kind=f"HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            stats.record((time.perf_counter() - t0) * 1000, ok=False, kind=type(e).__name__)


def worker_chat(base, token, stop_at, stats):
    """SSE 问答：一次完整流算一个请求。"""
    while time.time() < stop_at:
        t0 = time.perf_counter()
        try:
            r = urllib.request.Request(base + "/api/v1/chat",
                data=json.dumps({"query": "DAP 和 DDP 有什么区别？"}).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(r, timeout=180) as resp:
                for line in resp:
                    if line.startswith(b"event: done"):
                        break
            stats.record((time.perf_counter() - t0) * 1000)
        except urllib.error.HTTPError as e:
            stats.record((time.perf_counter() - t0) * 1000, ok=False, kind=f"HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            stats.record((time.perf_counter() - t0) * 1000, ok=False, kind=type(e).__name__)


def report(stats, label):
    lat = sorted(stats.lat)
    n = len(lat)
    if n == 0:
        print(f"[{label}] 0 成功 / {stats.errors} 失败")
        return
    def pct(p):
        return lat[min(n - 1, int(n * p))]
    print(f"[{label}] 成功 {n} | 失败 {stats.errors} | 错误率 {stats.errors / stats.count:.2%}")
    print(f"  P50={pct(0.50):.0f}ms  P95={pct(0.95):.0f}ms  P99={pct(0.99):.0f}ms  "
          f"mean={statistics.mean(lat):.0f}ms  max={lat[-1]:.0f}ms  "
          f"吞吐≈{n / (lat and 1) and n / stats_duration[0]:.1f} req/s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--scenario", choices=["read", "chat"], default="read")
    ap.add_argument("--concurrency", type=int, default=80)
    ap.add_argument("--seconds", type=int, default=120)
    args = ap.parse_args()

    token = None
    for attempt in range(5):
        try:
            token = login(args.base, "admin", "admin123")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"登录重试 {attempt + 1}/5：{exc}")
            time.sleep(3)
    if not token:
        print("登录失败，压测中止")
        return
    print(f"登录 OK | 场景={args.scenario} | 并发={args.concurrency} | 时长={args.seconds}s | 目标={args.base}")

    stats = Stats()
    stop_at = time.time() + args.seconds
    fn = worker_read if args.scenario == "read" else worker_chat
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(fn, args.base, token, stop_at, stats) for _ in range(args.concurrency)]
        for f in futures:
            f.result()
    duration = time.time() - started

    lat = sorted(stats.lat)
    n = len(lat)
    if n:
        def pct(p):
            return lat[min(n - 1, int(n * p))]
        print(f"\n===== 结果 [{args.scenario}] 并发={args.concurrency} 时长={duration:.0f}s =====")
        print(f"成功 {n} | 失败 {stats.errors} | 错误率 {stats.errors / stats.count:.2%}")
        if stats.err_kinds:
            print("错误分布:", stats.err_kinds)
        print(f"P50={pct(0.50):.0f}ms  P95={pct(0.95):.0f}ms  P99={pct(0.99):.0f}ms  "
              f"mean={statistics.mean(lat):.0f}ms  max={lat[-1]:.0f}ms")
        print(f"吞吐 = {n / duration:.1f} req/s")
    else:
        print("0 成功，检查服务状态")


if __name__ == "__main__":
    main()
