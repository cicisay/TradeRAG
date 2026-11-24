"""PostgreSQL 迁移端到端验证（M6 步骤 10-1 的验收脚本）。

为什么做成"一个脚本跑完全程"：
本机是开发环境，命令行工具的进程树在命令结束时会一并结束，所以不能
"先起 PG，再另开一条命令跑迁移"。这里把 启动 → 建库 → 迁移 → 搬数据 →
体检 → 序列验证 串在一次执行里，跑完为止。

用法（确保 data/_pgbin 已解压、data/pgdata 已 initdb）：
    python scripts/verify_pg.py                 # 全流程
    python scripts/verify_pg.py --skip-migrate  # 只建表 + 体检

验证点（每一条都对应一个真实会踩的坑）：
1. Alembic 能在 PG 上把 18 张表建出来（含 user 保留字、JSON、timestamptz）。
2. SQLite 全量数据能迁过来，行数逐表一致。
3. 迁移后**新插入**不报主键冲突（证明自增序列已重置到 max(id)）。
4. 时间列读出来是带时区的 UTC。
5. 体检脚本零严重问题。
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import psycopg  # noqa: E402

PORT = 55432
PG_ROOT = BACKEND_DIR / "data" / "_pgbin" / "pgsql"
PGDATA = BACKEND_DIR / "data" / "pgdata"
PGLOG = BACKEND_DIR / "data" / "pg.log"
ADMIN_DSN = f"postgresql://postgres@127.0.0.1:{PORT}/postgres"
RAG_DSN = f"postgresql://postgres@127.0.0.1:{PORT}/rag"
RAG_URL = f"postgresql+psycopg://postgres@127.0.0.1:{PORT}/rag"


def pg_exe(name: str) -> str:
    return str(PG_ROOT / "bin" / f"{name}.exe")


def port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def wait_ready(timeout: int = 60) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with psycopg.connect(ADMIN_DSN, connect_timeout=2):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def ensure_server() -> str:
    if port_open():
        return "already_running"
    # 注意：不能用 capture_output。postgres 守护进程会继承这些管道句柄，
    # 导致本进程"看起来永远没结束"（工具侧只能超时杀进程）。
    # pg_ctl 的 -l 已经把服务端日志写进文件，这里只需要退出码。
    proc = subprocess.run(
        [pg_exe("pg_ctl"), "-D", str(PGDATA), "-l", str(PGLOG),
         "-o", f"-p {PORT} -c listen_addresses=127.0.0.1", "-w", "start"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        print(f"   pg_ctl 退出码 {proc.returncode}，见 {PGLOG}")
    return "started" if wait_ready() else "failed"


def ensure_database() -> bool:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        exists = conn.execute("select 1 from pg_database where datname='rag'").fetchone()
        if not exists:
            conn.execute("CREATE DATABASE rag")
            return True
    return False


def alembic_upgrade() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"url={RAG_URL}", "upgrade", "head"],
        cwd=str(BACKEND_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    print("  alembic:", " | ".join(tail))
    if proc.returncode != 0:
        print((proc.stderr or "")[-1500:])
        raise SystemExit("alembic upgrade 失败")


def probe_sequence() -> dict:
    """序列探针：插一条 role 再回滚。序列未重置时这里必然 duplicate key。"""
    with psycopg.connect(RAG_DSN) as conn:
        try:
            row = conn.execute(
                "insert into role (code, name) values ('_seq_probe', '序列探针') returning id"
            ).fetchone()
            conn.rollback()
            return {"ok": True, "new_id": row[0]}
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def probe_timezone() -> dict:
    with psycopg.connect(RAG_DSN) as conn:
        row = conn.execute("select created_at from \"user\" order by id limit 1").fetchone()
        col_types = conn.execute(
            "select data_type from information_schema.columns "
            "where table_name='user' and column_name='created_at'"
        ).fetchone()
    if not row:
        return {"ok": None, "note": "表为空，跳过"}
    ts: datetime = row[0]
    return {
        "ok": ts.tzinfo is not None,
        "value": ts.isoformat(),
        "column_type": col_types[0] if col_types else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-migrate", action="store_true", help="不搬数据，只建表体检")
    ap.add_argument("--truncate", action="store_true", help="迁移前清空目标表")
    args = ap.parse_args()

    report: dict = {}
    print("① 启动 PostgreSQL …")
    state = ensure_server()
    print("   状态:", state)
    if state == "failed":
        print("   日志末尾：")
        print(PGLOG.read_text(encoding="utf-8", errors="replace")[-1200:])
        return 1

    print("② 准备数据库 …")
    report["database_created"] = ensure_database()

    print("③ 执行 Alembic 迁移 …")
    alembic_upgrade()

    from scripts.check_db import check as db_check

    print("④ 建表后体检 …")
    info = db_check(RAG_URL)
    report["schema"] = {
        "tables": info["tables_actual"], "expected": info["tables_expected"],
        "indexes": info["indexes"], "foreign_keys": info["foreign_keys"],
        "ok": info["ok"], "problems": info["problems"],
    }
    print(f"   表 {info['tables_actual']}/{info['tables_expected']}  索引 {info['indexes']}  外键 {info['foreign_keys']}  结论 {'通过' if info['ok'] else '有问题'}")

    if not args.skip_migrate:
        from app.config import get_settings
        from scripts.migrate_sqlite_to_pg import run as migrate_run

        source = get_settings().database_url
        print(f"⑤ 迁移数据（{source} → PG）…")
        m = migrate_run(source, RAG_URL, truncate=args.truncate)
        report["migrate"] = {
            "total_rows": m.get("total_rows"), "ok": m["ok"],
            "tables": [t for t in m["tables"] if not t.get("ok")],
            "sequences": m.get("sequences"),
        }
        print(f"   {m.get('total_rows')} 行；逐表一致：{'是' if m['ok'] else '否'}")
        for t in m["tables"]:
            if not t.get("ok"):
                print("   不一致:", t)

        print("⑥ 迁移后体检 …")
        info2 = db_check(RAG_URL)
        report["after_migrate"] = {"ok": info2["ok"], "problems": info2["problems"]}
        print(f"   结论 {'通过' if info2['ok'] else '有问题'}")

    print("⑦ 序列 / 时区探针 …")
    report["sequence"] = probe_sequence()
    report["timezone"] = probe_timezone()
    print("   序列:", report["sequence"])
    print("   时区:", report["timezone"])

    ok = (
        info["ok"]
        and report["sequence"].get("ok")
        and report["timezone"].get("ok") is not False
        and report.get("after_migrate", {}).get("ok", True)
        and (args.skip_migrate or report.get("migrate", {}).get("ok", False))
    )
    report["ok"] = bool(ok)
    out = BACKEND_DIR / "data" / "pg_verify_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n=== 总体：{'通过' if ok else '失败'} ===  报告 {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
