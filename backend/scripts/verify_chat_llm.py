"""端到端验证：确认「检索 → 拼 Prompt → 大模型生成 → SSE 流式返回」整条链路通了。

前置：后端已在 127.0.0.1:8000 运行，且在 .env 里配了某个厂商的 API Key。

用法：
    .venv/Scripts/python.exe scripts/verify_chat_llm.py
    .venv/Scripts/python.exe scripts/verify_chat_llm.py --query "你的问题"
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
LOGIN = {"username": "admin", "password": "admin123"}


def wait_ready(timeout: int = 60) -> str:
    """等服务起来（内存向量库首次启动要重建索引，可能慢几秒），返回 token。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.post(f"{BASE}/api/v1/auth/login", json=LOGIN, timeout=5)
            if r.status_code == 200:
                return r.json()["access_token"]
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    raise SystemExit(f"后端未就绪：请确认 {BASE} 已启动（data/uvicorn_dev.log 看日志）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="不锈钢法兰的材质和压力等级有什么要求？")
    args = ap.parse_args()

    token = wait_ready()
    headers = {"Authorization": f"Bearer {token}"}

    caps = httpx.get(f"{BASE}/api/v1/system/capabilities", headers=headers, timeout=10).json()
    llm = caps.get("llm", {})
    print(f"LLM      : provider={llm.get('provider')} configured={llm.get('configured')}")
    print(f"向量库    : {caps.get('vector_store')}")
    print(f"问题      : {args.query}\n")

    t0 = time.perf_counter()
    first_at: float | None = None
    answer: list[str] = []
    refs = 0
    done: dict = {}

    with httpx.stream(
        "POST", f"{BASE}/api/v1/chat",
        headers=headers, json={"query": args.query}, timeout=120,
    ) as resp:
        if resp.status_code != 200:
            resp.read()
            print(f"请求失败 [{resp.status_code}] {resp.text[:300]}")
            return 1
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
                continue
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if event == "delta":
                if first_at is None:
                    first_at = time.perf_counter() - t0
                piece = data.get("content", "")
                answer.append(piece)
                print(piece, end="", flush=True)
            elif event == "meta":
                refs = len(data.get("refs") or [])
            elif event == "done":
                done = data
            elif event == "error":
                print(f"\n[错误] {data}")

    total = time.perf_counter() - t0
    text = "".join(answer)
    print("\n")
    print(f"证据条数  : {refs}")
    print(f"首字延迟  : {f'{first_at * 1000:.0f} ms' if first_at else '未收到内容'}")
    print(f"总耗时    : {total * 1000:.0f} ms")
    print(f"答案字数  : {len(text)}")

    if not text:
        print("结论：没有生成任何内容（可能未配置 Key，或检索为空走了兜底）")
        return 1
    if "当前未配置大模型" in text:
        print("结论：仍是模板模式，检查 .env 的 Key 与后端是否已重启")
        return 1
    if first_at and first_at > 2.5:
        print("结论：链路通，但首字超过 2.5s（FR-209），可考虑 highspeed 模型")
        return 0
    print("结论：链路通，生成正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
