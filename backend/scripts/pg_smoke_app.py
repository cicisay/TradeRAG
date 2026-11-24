"""应用级 PostgreSQL 烟测（M6 步骤 10-1 收尾）。

与 verify_pg.py 的区别：
- verify_pg 关注"库对不对"（建表、搬数据、序列、时区列类型）。
- 这个脚本关注"应用在 PG 上能不能正常干活"：登录 → 查文档 → 检索 → 提问。
  它用 FastAPI 的 TestClient 直接驱动真实应用，连接串指向 PG，等价于
  "把 DATABASE_URL 换成 PG 后启动服务"，只是不用真的开端口。

注意：必须在 import app.* 之前设置环境变量（engine 在导入时创建，晚了就无效）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PG_URL = "postgresql+psycopg://postgres@127.0.0.1:55432/rag"
os.environ["DATABASE_URL"] = PG_URL
os.environ["APP_ENV"] = "prod"
os.environ["EMBED_PROVIDER"] = "hash"        # 烟测不加载 1GB 模型，只验链路
os.environ["RERANK_PROVIDER"] = "heuristic"
os.environ["LLM_PROVIDER"] = "none"
os.environ["METRICS_ENABLED"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

result: dict = {"steps": []}


def step(name: str, ok: bool, detail=None) -> None:
    result["steps"].append({"step": name, "ok": bool(ok), "detail": detail})
    print(f"  {'OK ' if ok else 'ERR'} {name}" + (f"  {detail}" if detail else ""))


def main() -> int:
    with TestClient(app) as client:
        # 1) 数据库自检接口：证明应用连的是 PG，且迁移版本正确
        admin = client.post("/api/v1/auth/login",
                            json={"username": "admin", "password": "admin123"})
        step("管理员登录", admin.status_code == 200, admin.status_code)
        if admin.status_code != 200:
            return 1
        headers = {"Authorization": f"Bearer {admin.json()['access_token']}"}

        db = client.get("/api/v1/system/db", headers=headers).json()
        step("system/db 指向 PG",
             db.get("backend") == "postgresql" and db.get("alembic_revision") == "0001",
             f"backend={db.get('backend')} rev={db.get('alembic_revision')} "
             f"tz={db.get('server_timezone')} tables={db.get('tables')}")

        # 2) 存量数据可见（迁移过来的 23 篇文档）
        docs = client.get("/api/v1/documents", headers=headers).json()
        items = docs if isinstance(docs, list) else docs.get("items", [])
        step("文档列表非空", len(items) > 0, f"{len(items)} 篇")

        # 3) 检索：证明 chunk 表与权限过滤在 PG 上工作
        r = client.post("/api/v1/debug/retrieval", headers=headers,
                        json={"query": "不锈钢法兰的材质和压力等级", "top_k": 5})
        data = r.json()
        hit = len(data.get("hybrid", []))
        step("检索有召回", r.status_code == 200 and hit > 0,
             f"status={r.status_code} hits={hit} took={data.get('took_ms')}ms")

        # 4) 提问：走完整链路（合规→SQL→检索→生成兜底），消息落 PG
        with client.stream("POST", "/api/v1/chat", headers=headers,
                           json={"query": "请说明报关需要哪些单证"}) as resp:
            events = []
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    events.append(line.split(":", 1)[1].strip())
        step("SSE 问答完成", "done" in events, f"events={events[:6]}")

        # 5) 时间戳：应用侧读出来必须是 UTC（连接池里设置了 timezone=UTC）
        r_sess = client.get("/api/v1/sessions", headers=headers)
        msgs = r_sess.json()
        sessions = msgs if isinstance(msgs, list) else msgs.get("items", [])
        step("会话已落库", r_sess.status_code == 200 and len(sessions) > 0,
             f"status={r_sess.status_code} n={len(sessions)} raw={str(msgs)[:160]}")

        from sqlalchemy import text
        from app.db.session import engine
        with engine.connect() as conn:
            ts = conn.execute(text('select created_at from "message" order by id desc limit 1')).scalar()
            tzname = conn.execute(text("show timezone")).scalar()
        step("应用连接时区为 UTC", tzname == "UTC" and ts.utcoffset().total_seconds() == 0,
             f"timezone={tzname} value={ts.isoformat() if ts else None}")

    ok = all(s["ok"] for s in result["steps"])
    result["ok"] = ok
    out = BACKEND / "data" / "pg_smoke_report.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=== 应用级烟测：{'通过' if ok else '失败'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
