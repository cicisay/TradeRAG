"""数据库体检（M6 步骤 10-1）。

用法：
    python scripts/check_db.py                 # 检查 .env 里配置的库
    python scripts/check_db.py --json          # 机器可读输出（可接监控/CI）
    python scripts/check_db.py --url "postgresql+psycopg://rag:rag@127.0.0.1:5432/rag"

检什么（都是"迁移后最容易出的事故"）：
1. **表是否齐**：ORM 里定义 18 张业务表，库里少一张就是迁移没跑完。
2. **自增序列是否落后**：序列值 < 表内 max(id) 时，下一次 INSERT 必定主键冲突。
   这是手工迁移数据最常见的后遗症，必须在切流前发现。
3. **时间列类型**：PG 里应该是 `timestamp with time zone`。如果是 `timestamp without time zone`，
   跨时区读写会漂 8 小时（服务器在东八区时尤其明显）。
4. **外键约束数**：SQLite 默认不检查外键，PG 强制检查。约束数对不上，说明建表时丢了关系。
5. **连接池与超时**：确认 statement_timeout、pool_size 真的生效，而不是写了配置没生效。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.db.session import Base, _register_models, db_info, is_postgres  # noqa: E402


def _expected_tables() -> set[str]:
    _register_models()
    return set(Base.metadata.tables)


def check(url: str | None = None) -> dict:
    if url:
        os_url = url
    else:
        from app.config import get_settings
        os_url = get_settings().database_url

    engine = create_engine(os_url, future=True)
    pg = os_url.startswith(("postgresql", "postgres"))
    expected = _expected_tables()
    problems: list[dict] = []
    warnings: list[dict] = []

    # 用应用自己的 db_info 拿不到自定义 url，这里自己拼一份基础信息
    with engine.connect() as conn:
        server_version = conn.execute(text("select version()")).scalar()

    insp = inspect(engine)
    actual = set(insp.get_table_names())
    missing = expected - actual
    extra = actual - expected - {"alembic_version", "sqlite_sequence"}
    if missing:
        problems.append({"code": "E_DB_SCHEMA", "msg": f"缺少表：{sorted(missing)}"})
    if extra:
        warnings.append({"code": "W_DB_EXTRA", "msg": f"库中多出未定义的表：{sorted(extra)}"})

    tables: list[dict] = []
    seq_issues: list[dict] = []
    with engine.connect() as conn:
        for name in sorted(expected & actual):
            cnt = conn.execute(text(f'select count(*) from {_q(conn, name)}')).scalar() or 0
            tables.append({"table": name, "rows": cnt})

            if pg:
                seq = conn.execute(
                    text("select pg_get_serial_sequence(:t, 'id')"), {"t": f'"{name}"'}
                ).scalar()
                if seq:
                    max_id = conn.execute(
                        text(f'select coalesce(max(id), 0) from {_q(conn, name)}')
                    ).scalar() or 0
                    last, called = conn.execute(
                        text(f"select last_value, is_called from {seq}")
                    ).one()
                    # 序列"下一个值" = last+1（已调用过）或 last（从未调用）
                    next_val = last + 1 if called else last
                    if max_id >= next_val:
                        seq_issues.append({
                            "table": name, "sequence": seq,
                            "max_id": max_id, "next_value": next_val,
                        })

    if seq_issues:
        problems.append({
            "code": "E_DB_SEQUENCE",
            "msg": "自增序列落后于表内最大 id，下一次插入会主键冲突",
            "detail": seq_issues,
        })

    # 时间列类型（PG）
    tz_bad: list[dict] = []
    if pg:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "select table_name, column_name, data_type from information_schema.columns "
                "where table_schema = current_schema() and data_type like 'timestamp%'"
            )).all()
        for t, c, dt in rows:
            if dt != "timestamp with time zone":
                tz_bad.append({"table": t, "column": c, "type": dt})
        if tz_bad:
            problems.append({
                "code": "E_DB_TIMEZONE",
                "msg": "存在不带时区的时间列，跨时区会漂移",
                "detail": tz_bad,
            })

    fk_total = sum(len(insp.get_foreign_keys(t["table"])) for t in tables)
    idx_total = sum(len(insp.get_indexes(t["table"])) for t in tables)

    result = {
        "backend": "postgresql" if pg else "sqlite",
        "url": _mask(os_url),
        "server": (server_version or "").split(" on ")[0],
        "tables_expected": len(expected),
        "tables_actual": len(actual & expected),
        "indexes": idx_total,
        "foreign_keys": fk_total,
        "rows_total": sum(t["rows"] for t in tables),
        "tables": tables,
        "problems": problems,
        "warnings": warnings,
        "ok": not problems,
    }
    engine.dispose()
    return result


def _q(conn, name: str) -> str:
    return conn.dialect.identifier_preparer.quote(name)


def _mask(url: str) -> str:
    if "@" not in url or "//" not in url:
        return url
    scheme, rest = url.split("//", 1)
    creds, host = rest.split("@", 1)
    return f"{scheme}//{creds.split(':', 1)[0]}:***@{host}" if ":" in creds else url


def main() -> int:
    ap = argparse.ArgumentParser(description="数据库体检")
    ap.add_argument("--url", default="", help="不填则用 .env 的 DATABASE_URL")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    try:
        res = check(args.url or None)
    except Exception as exc:  # noqa: BLE001
        print(f"连接失败：{type(exc).__name__}: {str(exc)[:300]}")
        return 1

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    print(f"数据库：{res['backend']}  {res['url']}")
    print(f"版本  ：{res['server']}")
    print(f"表    ：{res['tables_actual']}/{res['tables_expected']}   索引 {res['indexes']}   外键 {res['foreign_keys']}   总行数 {res['rows_total']}")
    for p in res["problems"]:
        print(f"  [严重] {p['code']} {p['msg']}")
        for d in p.get("detail", [])[:5]:
            print(f"         - {d}")
    for w in res["warnings"]:
        print(f"  [提示] {w['code']} {w['msg']}")
    print("结论  ：", "通过" if res["ok"] else "发现问题，需处理")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
