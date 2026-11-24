"""SQLite → PostgreSQL 数据迁移（M6 步骤 10-1）。

用法：
    # 1) 先演练，不写目标库（看每张表要迁多少行、有没有类型转换失败）
    python scripts/migrate_sqlite_to_pg.py --target "postgresql+psycopg://rag:rag@127.0.0.1:5432/rag" --dry-run

    # 2) 正式迁移（目标库表结构需已存在：先 alembic upgrade head）
    python scripts/migrate_sqlite_to_pg.py --target "postgresql+psycopg://rag:rag@127.0.0.1:5432/rag"

    # 3) 覆盖式重迁（先清空目标表，慎用）
    python scripts/migrate_sqlite_to_pg.py --target "..." --truncate

    # 4) 只校验（迁完或怀疑不一致时）
    python scripts/migrate_sqlite_to_pg.py --target "..." --verify-only

为什么不能直接"复制粘贴数据库文件"：
1. **类型语义不同**：SQLite 的布尔是 0/1，PG 的 boolean 只认 true/false；SQLite 的时间是
   一段没有时区的文本，PG 是 timestamptz。硬塞会在第一行就报类型错误。
2. **JSON 会被双重编码**：SQLite 里 JSON 列存的是文本 '["zh"]'，如果原样插进 PG 的 JSON 列，
   SQLAlchemy 会再 dumps 一次，变成 '"[\"zh\"]"' —— 看着有数据，程序读出来是字符串。
3. **自增序列不会自动跟上**：手工插入保留原 id 后，PG 的序列仍停在 1。
   结果是"迁移后第一次新建用户就报主键冲突"。必须 setval 到 max(id)。这是最经典的迁移事故。
4. **外键顺序**：必须先插父表（role）再插子表（user），否则 PG 直接拒绝。

脚本做法：读源库 → 按目标列类型逐值转换 → 按外键拓扑顺序批量插入 → 重置序列 → 校验行数。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, JSON, MetaData, Table, create_engine, func, select, text
from sqlalchemy.engine import Engine

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402

SKIP_TABLES = {"alembic_version", "sqlite_sequence"}
BATCH = 500


# ---------------------------------------------------------------- 类型转换
def convert_value(value, col) -> object:
    """把 SQLite 读出来的值转换成 PostgreSQL 列能接受的值。"""
    if value is None:
        return None
    t = col.type

    if isinstance(t, Boolean):
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "", "no")
        return bool(value)

    if isinstance(t, DateTime):
        if isinstance(value, datetime):
            dt = value
        else:
            s = str(value).strip().replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                return None
        # SQLite 存的是 UTC 但丢了时区标记：按 UTC 补回，PG 落库为 timestamptz
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    if isinstance(t, Date) and not isinstance(t, DateTime):
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None

    if isinstance(t, JSON):
        if isinstance(value, (str, bytes)):
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return None
        return value

    return value


# ---------------------------------------------------------------- 工具
def quote(conn, name: str) -> str:
    return conn.dialect.identifier_preparer.quote(name)


def table_counts(engine: Engine, names: list[str], prefix: str) -> dict[str, int]:
    out: dict[str, int] = {}
    with engine.connect() as conn:
        for n in names:
            try:
                out[n] = conn.execute(text(f"select count(*) from {quote(conn, n)}")).scalar() or 0
            except Exception:  # noqa: BLE001  表不存在
                out[n] = -1
    return out


def reset_sequences(conn, tables: list[str]) -> list[dict]:
    """把 PG 自增序列推到当前 max(id)。

    不做这一步，迁移后第一次 INSERT 就会主键冲突（序列从 1 开始，而 1 已被占用）。
    """
    results = []
    for name in tables:
        qt = quote(conn, name)
        try:
            seq = conn.execute(
                text("select pg_get_serial_sequence(:t, 'id')"), {"t": f'"{name}"'}
            ).scalar()
            if not seq:
                continue
            max_id = conn.execute(text(f"select coalesce(max(id), 0) from {qt}")).scalar() or 0
            # 表为空时设 1 且 is_called=false，否则从 1 开始；有数据则从 max(id) 之后开始
            conn.execute(
                text("select setval(:s, :v, :called)"),
                {"s": seq, "v": max(max_id, 1), "called": max_id > 0},
            )
            results.append({"table": name, "sequence": seq, "max_id": max_id})
        except Exception as exc:  # noqa: BLE001
            results.append({"table": name, "error": str(exc)[:200]})
    return results


# ---------------------------------------------------------------- 主流程
def run(
    source_url: str,
    target_url: str,
    *,
    dry_run: bool = False,
    truncate: bool = False,
    verify_only: bool = False,
) -> dict:
    src = create_engine(source_url, future=True)
    dst = create_engine(target_url, future=True)

    src_meta = MetaData()
    src_meta.reflect(bind=src)
    dst_meta = MetaData()
    dst_meta.reflect(bind=dst)

    src_tables = {t.name for t in src_meta.tables.values()} - SKIP_TABLES
    dst_tables = set(dst_meta.tables) - SKIP_TABLES
    common = src_tables & dst_tables
    missing = sorted(src_tables - dst_tables)
    if missing:
        raise SystemExit(
            f"目标库缺少表：{missing}\n请先执行：alembic -x url={target_url} upgrade head"
        )

    # 外键拓扑顺序：父表在前（sorted_tables 已按依赖排序）
    ordered = [t.name for t in dst_meta.sorted_tables if t.name in common and t.name not in SKIP_TABLES]

    src_counts = table_counts(src, ordered, "sqlite")
    report: dict = {
        "source": _mask(source_url),
        "target": _mask(target_url),
        "dry_run": dry_run,
        "verify_only": verify_only,
        "tables": [],
    }

    if verify_only:
        dst_counts = table_counts(dst, ordered, "pg")
        for name in ordered:
            row = {"table": name, "source": src_counts[name], "target": dst_counts[name]}
            row["ok"] = src_counts[name] == dst_counts[name]
            report["tables"].append(row)
        report["ok"] = all(r["ok"] for r in report["tables"])
        return report

    total_rows = 0
    with dst.begin() as dconn:
        if truncate and not dry_run:
            names = ", ".join(quote(dconn, n) for n in reversed(ordered))
            dconn.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))

        for name in ordered:
            s_table: Table = src_meta.tables[name]
            d_table: Table = dst_meta.tables[name]
            d_cols = {c.name: c for c in d_table.columns}

            with src.connect() as sconn:
                rows = sconn.execute(select(s_table)).mappings().all()

            payload, errors = [], 0
            for row in rows:
                item = {}
                for key, value in row.items():
                    if key not in d_cols:
                        continue  # 源有、目标没有的旧列直接丢弃
                    try:
                        item[key] = convert_value(value, d_cols[key])
                    except Exception:  # noqa: BLE001
                        errors += 1
                        item[key] = None
                payload.append(item)

            if not dry_run and payload:
                for i in range(0, len(payload), BATCH):
                    dconn.execute(d_table.insert(), payload[i:i + BATCH])

            total_rows += len(payload)
            report["tables"].append({
                "table": name, "source": src_counts[name], "read": len(payload),
                "convert_errors": errors,
            })

        seq_report = [] if dry_run else reset_sequences(dconn, ordered)

    report["total_rows"] = total_rows
    report["sequences"] = seq_report

    if not dry_run:
        dst_counts = table_counts(dst, ordered, "pg")
        for row in report["tables"]:
            row["target"] = dst_counts[row["table"]]
            row["ok"] = dst_counts[row["table"]] == row["source"]
        report["ok"] = all(r.get("ok") for r in report["tables"])
    else:
        report["ok"] = True
    return report


def _mask(url: str) -> str:
    if "@" not in url or "//" not in url:
        return url
    scheme, rest = url.split("//", 1)
    creds, host = rest.split("@", 1)
    return f"{scheme}//{creds.split(':', 1)[0]}:***@{host}" if ":" in creds else url


def main() -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser(description="SQLite → PostgreSQL 数据迁移")
    ap.add_argument("--source", default=settings.database_url, help="源 SQLite 连接串")
    ap.add_argument("--target", required=True, help="目标 PG 连接串（postgresql+psycopg://...）")
    ap.add_argument("--dry-run", action="store_true", help="只演练，不写目标库")
    ap.add_argument("--truncate", action="store_true", help="先清空目标库所有业务表（危险）")
    ap.add_argument("--verify-only", action="store_true", help="只对比行数，不迁移")
    ap.add_argument("--report", default="", help="把结果 JSON 写到指定文件")
    args = ap.parse_args()

    if not args.source.startswith("sqlite"):
        print("源库必须是 SQLite，当前为：", args.source)
        return 2
    if not args.target.startswith(("postgresql", "postgres")):
        print("目标库必须是 PostgreSQL，当前为：", args.target)
        return 2

    try:
        report = run(
            args.source, args.target,
            dry_run=args.dry_run, truncate=args.truncate, verify_only=args.verify_only,
        )
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"迁移失败：{type(exc).__name__}: {exc}")
        return 1

    print(f"\n{'表':<20}{'源':>8}{'目标':>8}  状态")
    for row in report["tables"]:
        mark = "OK" if row.get("ok") else ("跳过" if report["dry_run"] else "不一致")
        print(f"{row['table']:<20}{row['source']:>8}{str(row.get('target', '-')):>8}  {mark}")
    if report.get("sequences"):
        print("\n序列已重置：", ", ".join(
            f"{s['table']}={s.get('max_id')}" for s in report["sequences"] if "error" not in s
        ))
    print(f"\n合计 {report.get('total_rows', '-')} 行；整体：{'通过' if report['ok'] else '需检查'}")

    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("报告已写入", args.report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
