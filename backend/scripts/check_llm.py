"""LLM 接入自检脚本（新手友好）。

用法：
    .venv/Scripts/python.exe scripts/check_llm.py

它做三件事：
1. 打印当前读到的 provider / model / base_url（确认 .env 生效）
2. 发一条最小流式请求，测首字延迟与总耗时（对应 FR-209 首字 P95≤2.5s）
3. 发一条非流式请求，测合规语义兜底用的 chat_once 通道

任何一种失败都会打印具体原因（401 是 Key 错、404 是 base_url 或 model 名错、
ConnectError 是本机服务没起来 / 代理拦了），方便对症改配置。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# 允许直接 python scripts/xxx.py 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.llm import get_llm_client  # noqa: E402


def main() -> int:
    client = get_llm_client()
    client.resolve()  # 无数据库上下文时按 .env 构建降级链
    print("=" * 56)
    chain = client.chain
    print(f"provider : {client.provider}（可降级 {len(chain)} 家）")
    for i, e in enumerate(chain, 1):
        key = e.api_key or ""
        mask = f"****{key[-4:]}" if len(key) > 8 else "(未配置)"
        print(f"  {i}. {e.provider:9s} {e.model:24s} {e.base_url}")
        print(f"     api_key: {mask}")
    if not chain:
        print("  （无可用提供商）")
    print("=" * 56)

    if not client.available:
        print("\n[未接入] 现在后端处于“模板模式”：只回检索依据，不生成答案。")
        print("在 backend/.env 里填任意一种即可：")
        print("  DeepSeek: DEEPSEEK_API_KEY=sk-xxxx")
        print("  Qwen    : QWEN_API_KEY=sk-xxxx")
        print("  Ollama  : DEEPSEEK_API_KEY=ollama")
        print("            DEEPSEEK_BASE_URL=http://127.0.0.1:11434/v1")
        print("            DEEPSEEK_MODEL=qwen2.5:7b")
        return 1

    ok = True

    print("\n[1/2] 流式对话 …")
    t0 = time.perf_counter()
    first_at = None
    chars = 0
    try:
        for delta in client.stream_chat(
            [{"role": "user", "content": "用一句话说明什么是RAG，不要超过30字。"}]
        ):
            if first_at is None:
                first_at = time.perf_counter() - t0
            chars += len(delta)
            print(delta, end="", flush=True)
        total = time.perf_counter() - t0
        print()
        print(f"  ✔ 首字 {first_at * 1000:.0f} ms / 总耗时 {total:.2f} s / 共 {chars} 字")
        if first_at and first_at > 2.5:
            print("  ⚠ 首字超过 2.5s（FR-209 目标），检查网络或换更近的接入点")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  ✘ 失败: {type(exc).__name__}: {exc}")
        _hint(exc)

    print("\n[2/2] 非流式单次调用（合规兜底通道）…")
    try:
        t0 = time.perf_counter()
        text = client.chat_once([{"role": "user", "content": "回复两个字：正常"}])
        print(f"  ✔ {text.strip()[:40]}  ({(time.perf_counter() - t0) * 1000:.0f} ms)")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  ✘ 失败: {type(exc).__name__}: {exc}")
        _hint(exc)

    print()
    print("结论：", "接入成功，重启后端即可看到生成式回答。" if ok else "接入未通过，按上面的提示改 .env 后重跑。")
    return 0 if ok else 1


def _hint(exc: Exception) -> None:
    msg = str(exc)
    if "401" in msg or "Unauthorized" in msg:
        print("    → Key 无效或已过期，重新生成一个再填")
    elif "402" in msg:
        print("    → 账户余额不足")
    elif "404" in msg:
        print("    → base_url 少了 /v1 或 model 名写错（DeepSeek 用 deepseek-chat）")
    elif "429" in msg:
        print("    → 触发限流，稍等再试")
    elif "ConnectError" in type(exc).__name__ or "connect" in msg.lower():
        print("    → 连不上服务：本地 Ollama 没启动，或公司网络/代理挡了外网")
    elif "timeout" in type(exc).__name__.lower():
        print("    → 超时：网络慢，或 max_tokens 太大")


if __name__ == "__main__":
    raise SystemExit(main())
