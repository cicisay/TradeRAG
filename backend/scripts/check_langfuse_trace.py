"""11-1 验收脚本：发一次真实问答，再回读 Langfuse 里的 trace 树。

为什么需要它：界面上"肉眼看一下"没法进 CI，也没法说清"到底几个 span 落了"。
本脚本把验收变成可重复的两步——先真的问一句，再用 Langfuse 公共 API
（Basic Auth = public_key:secret_key）把这条 trace 的观测节点全拉出来打印。

用法（宿主机，服务已在 18080/13000 起来）：
    backend/.venv/Scripts/python.exe scripts/check_langfuse_trace.py
    ... --query "不锈钢法兰的材质和压力等级有什么要求？"
    ... --user demo_sales --password demo123456

退出码：0 = trace 落库且关键阶段齐全；1 = 没落库 / 阶段缺失（打印原因）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# trace 里必须出现的阶段（缺一个就说明埋点断了，不只是"没数据"）
REQUIRED = ("compliance.precheck", "retrieval", "llm.stream")


def login(base: str, user: str, password: str,
          captcha_id: str | None = None, captcha_text: str | None = None) -> str:
    payload: dict = {"username": user, "password": password}
    if captcha_id and captcha_text:
        payload.update(captcha_id=captcha_id, captcha_text=captcha_text)
    r = httpx.post(f"{base}/api/v1/auth/login", json=payload, timeout=15)
    if r.status_code == 401 and "E6005" in r.text:
        raise SystemExit(
            "登录要图形验证码（LOGIN_CAPTCHA_ENABLED=true）。两种办法：\n"
            "  a) 传 --captcha-id/--captcha-text：先在容器里取一张\n"
            "     docker exec foreign-trade-rag-api-1 python -c \"from app.services.captcha import new_captcha; print(new_captcha()[0], new_captcha()[2])\"  # 注意同一次调用\n"
            "  b) 临时把 .env 里 LOGIN_CAPTCHA_ENABLED 设为 false 再重跑（测完改回来）"
        )
    r.raise_for_status()
    return r.json()["access_token"]


def ask(base: str, token: str, query: str) -> dict:
    """发一次流式问答，返回 request_id / 字数 / 引用数 / 首字延迟。"""
    t0 = time.perf_counter()
    first_at: float | None = None
    answer: list[str] = []
    refs = 0
    done: dict = {}
    with httpx.stream("POST", f"{base}/api/v1/chat",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"query": query}, timeout=180) as resp:
        if resp.status_code != 200:
            resp.read()
            raise SystemExit(f"问答请求失败 [{resp.status_code}] {resp.text[:300]}")
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
                answer.append(data.get("content", ""))
            elif event == "meta":
                refs = len(data.get("refs") or [])
            elif event == "done":
                done = data
    return {
        "request_id": done.get("request_id", ""),
        "chars": len("".join(answer)),
        "refs": refs,
        "first_token_ms": round((first_at or 0) * 1000),
        "total_ms": round((time.perf_counter() - t0) * 1000),
    }


def lf_headers() -> dict:
    pk = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    sk = os.getenv("LANGFUSE_SECRET_KEY", "")
    if not (pk and sk):
        raise SystemExit(".env 里缺 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")
    raw = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def fetch_trace(lf: str, trace_id: str, *, timeout: float = 25.0) -> dict | None:
    """轮询等 trace 落库（SDK 是异步 flush，秒级延迟属正常）。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        r = httpx.get(f"{lf}/api/public/traces/{trace_id}", headers=lf_headers(), timeout=15)
        if r.status_code == 200:
            return r.json()
        last = f"[{r.status_code}] {r.text[:160]}"
        time.sleep(1.5)
    print(f"未取到 trace：{last}")
    return None


def print_tree(tr: dict) -> list[str]:
    obs = tr.get("observations") or []
    # 父节点先出：按 parent 分组，从根（parentObservationId 为空）开始深度优先
    children: dict[str | None, list[dict]] = {}
    for o in obs:
        children.setdefault(o.get("parentObservationId"), []).append(o)
    for lst in children.values():
        lst.sort(key=lambda x: x.get("startTime") or "")

    names: list[str] = []

    def walk(parent: str | None, depth: int) -> None:
        for o in children.get(parent, []):
            took = ""
            if o.get("startTime") and o.get("endTime"):
                from datetime import datetime
                def p(s: str) -> datetime:
                    return datetime.fromisoformat(s.replace("Z", "+00:00"))
                took = f"{(p(o['endTime']) - p(o['startTime'])).total_seconds() * 1000:.0f} ms"
            model = f" model={o['model']}" if o.get("model") else ""
            meta = (o.get("metadata") or {})
            extra = ""
            if meta.get("first_token_ms"):
                extra = f" 首字={meta['first_token_ms']}ms"
            if meta.get("took_ms"):
                extra += f" took={meta['took_ms']}ms"
            print(f"{'  ' * depth}├─ {o.get('type'):<10} {o.get('name')}  {took}{model}{extra}")
            names.append(o.get("name") or "")
            walk(o.get("id"), depth + 1)

    print(f"trace: {tr.get('name')}  id={tr.get('id')}  user={tr.get('userId')}")
    print(f"  input : {str(tr.get('input'))[:90]}")
    print(f"  output: {str(tr.get('output'))[:90]}")
    walk(None, 1)
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18080")
    ap.add_argument("--lf", default="http://127.0.0.1:13000")
    ap.add_argument("--user", default="demo_sales")
    ap.add_argument("--password", default="demo123456")
    ap.add_argument("--query", default="不锈钢法兰的材质和压力等级有什么要求？")
    ap.add_argument("--captcha-id", default=None, help="登录验证码 id（开启验证码时需要）")
    ap.add_argument("--captcha-text", default=None, help="登录验证码文字")
    ap.add_argument("--wait", type=float, default=25.0)
    args = ap.parse_args()

    token = login(args.base, args.user, args.password,
                  args.captcha_id, args.captcha_text)
    print(f"问题: {args.query}")
    res = ask(args.base, token, args.query)
    print(f"答案 {res['chars']} 字 / 引用 {res['refs']} 条 / 首字 {res['first_token_ms']}ms "
          f"/ 总 {res['total_ms']}ms")
    print(f"request_id = {res['request_id']}")

    if not res["request_id"]:
        print("结论：done 事件里没有 request_id，无法对照 trace")
        return 1

    tr = fetch_trace(args.lf, res["request_id"], timeout=args.wait)
    if not tr:
        print("结论：Langfuse 里没有这条 trace（检查 LANGFUSE_HOST / key / SAMPLE_RATE）")
        return 1

    names = print_tree(tr)
    missing = [n for n in REQUIRED if n not in names]
    if missing:
        print(f"结论：trace 已落库，但缺阶段 {missing}（埋点被跳过或采样未命中）")
        return 1
    print(f"结论：trace 落库，共 {len(names)} 个节点，关键阶段齐全 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
