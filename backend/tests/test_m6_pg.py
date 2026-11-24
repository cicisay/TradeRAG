"""M6 步骤 10-1 测试：PostgreSQL 兼容性（不依赖真实 PG 也能跑的部分）。

为什么这些测试值得写：
- "改完 SQLite 测试全绿"不代表能在 PG 上跑。方言差异（保留字、类型、时区、序列）
  只有在专门的断言里才会暴露。
- 这里用 SQLAlchemy 的 PG 方言**离线编译**建表语句，能在没有 PG 服务的机器上
  提前发现 80% 的兼容性问题（比如 user 是 PG 保留字、JSON 列类型是否正确）。
- 真实 PG 的端到端用例在 test_m6_pg_live.py，需要设置 TEST_PG_URL 才执行。
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import JSON, Boolean, Column, Date, DateTime, Integer, MetaData, Table, create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.db.session import Base, _register_models, db_info, engine
from app.db.types import TZDateTime
from scripts.migrate_sqlite_to_pg import convert_value


# ---------------------------------------------------------------- 时区类型
class TestTZDateTime:
    @pytest.fixture()
    def probe(self):
        """临时探针表：验证 TZDateTime 在 SQLite 上的读写行为。"""
        md = MetaData()
        t = Table(
            "_tz_probe", md,
            Column("id", Integer, primary_key=True),
            Column("ts", TZDateTime),
        )
        md.create_all(engine)
        yield t
        md.drop_all(engine)

    def _roundtrip(self, t, value):
        from sqlalchemy import insert, select
        with engine.begin() as conn:
            conn.execute(insert(t).values(id=1, ts=value))
            got = conn.execute(select(t.c.ts).where(t.c.id == 1)).scalar()
            conn.execute(t.delete())
        return got

    def test_aware_utc_preserved(self, probe):
        """写入 aware UTC，读回来仍是 aware UTC（不会退化成 naive）。"""
        src = datetime(2026, 11, 9, 6, 30, tzinfo=timezone.utc)
        got = self._roundtrip(probe, src)
        assert got.tzinfo is not None, "读回来的时间丢了时区，PG 上会引发 aware/naive 比较异常"
        assert got.utcoffset() == timedelta(0)
        assert got.replace(tzinfo=None) == src.replace(tzinfo=None)

    def test_naive_treated_as_utc(self, probe):
        """写入 naive 时间按 UTC 解释（兼容历史数据）。"""
        got = self._roundtrip(probe, datetime(2026, 1, 2, 3, 4, 5))
        assert got.tzinfo == timezone.utc
        assert got.hour == 3

    def test_other_timezone_normalized_to_utc(self, probe):
        """东八区时间入库后统一成 UTC（06:00+08:00 == 前一天 22:00 UTC）。"""
        src = datetime(2026, 11, 9, 6, 0, tzinfo=timezone(timedelta(hours=8)))
        got = self._roundtrip(probe, src)
        assert got == src  # 同一时刻
        assert got.hour == 22 and got.day == 8


# ---------------------------------------------------------------- PG 方言编译
class TestPostgresDdl:
    @pytest.fixture(autouse=True)
    def _models(self):
        _register_models()

    def _ddl(self, table: str) -> str:
        return str(CreateTable(Base.metadata.tables[table]).compile(dialect=postgresql.dialect()))

    def test_reserved_word_user_is_quoted(self):
        """user 是 PostgreSQL 保留字，必须加双引号，否则建表直接语法错误。"""
        ddl = self._ddl("user")
        assert 'CREATE TABLE "user"' in ddl

    def test_all_tables_compile_for_postgres(self):
        """18 张业务表都能编译成 PG DDL（少一张说明模型漏注册）。"""
        _register_models()
        assert len(Base.metadata.tables) >= 18
        for name in Base.metadata.tables:
            ddl = self._ddl(name)
            assert "CREATE TABLE" in ddl

    def test_timestamp_columns_are_timestamptz(self):
        """时间列必须是 timestamp with time zone，否则跨时区会漂 8 小时。"""
        for name in ("user", "message", "doc_version", "badcase"):
            assert "TIMESTAMP WITH TIME ZONE" in self._ddl(name)

    def test_boolean_and_json_types(self):
        assert "BOOLEAN" in self._ddl("doc_version")
        assert "JSON" in self._ddl("document_meta")  # languages 列

    def test_primary_key_autoincrement(self):
        """PG 用 SERIAL 实现自增（迁移后必须重置序列，见迁移脚本）。"""
        assert "SERIAL" in self._ddl("user")


# ---------------------------------------------------------------- 迁移值转换
class TestMigrationConvert:
    @pytest.fixture()
    def cols(self):
        md = MetaData()
        t = Table(
            "conv", md,
            Column("b", Boolean), Column("ts", DateTime(timezone=True)),
            Column("d", Date), Column("j", JSON), Column("n", Integer),
        )
        return t.c

    def test_bool_from_sqlite_int(self, cols):
        assert convert_value(1, cols.b) is True
        assert convert_value(0, cols.b) is False

    def test_bool_from_text(self, cols):
        assert convert_value("1", cols.b) is True
        assert convert_value("0", cols.b) is False

    def test_datetime_string_gets_utc(self, cols):
        """SQLite 存的是 '2026-11-09 03:57:47.038496'（无时区），迁移后必须补 UTC。"""
        got = convert_value("2026-11-09 03:57:47.038496", cols.ts)
        assert isinstance(got, datetime)
        assert got.tzinfo == timezone.utc
        assert got.hour == 3

    def test_datetime_iso_with_z(self, cols):
        got = convert_value("2026-11-09T03:57:47Z", cols.ts)
        assert got.tzinfo is not None and got.hour == 3

    def test_date_string(self, cols):
        assert convert_value("2026-05-01", cols.d) == date(2026, 5, 1)

    def test_json_string_is_parsed_not_double_encoded(self, cols):
        """SQLite 里 JSON 列存文本；原样传会被 JSON 类型二次 dumps 成字符串。"""
        assert convert_value('["zh","en"]', cols.j) == ["zh", "en"]
        assert convert_value("[]", cols.j) == []

    def test_none_and_scalar_passthrough(self, cols):
        assert convert_value(None, cols.b) is None
        assert convert_value(42, cols.n) == 42


# ---------------------------------------------------------------- 自检接口
class TestDbInfo:
    def test_sqlite_info(self, client):
        info = db_info()
        assert info["ok"] is True
        assert info["backend"] == "sqlite"
        assert info["tables"] >= 18
        # 外键必须开启，否则本地与生产约束行为不一致
        assert info["foreign_keys"] == 1

    def test_system_db_endpoint(self, client, admin_headers):
        resp = client.get("/api/v1/system/db", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["backend"] in ("sqlite", "postgresql")
        assert data["ok"] is True

    def test_system_db_requires_audit_permission(self, client, sales_headers):
        resp = client.get("/api/v1/system/db", headers=sales_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------- Alembic
class TestAlembic:
    def test_initial_revision_exists(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "migrations")
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        assert len(heads) == 1, f"迁移头应只有一个，实际 {heads}"

    def test_revision_creates_all_tables(self):
        """迁移脚本必须覆盖所有 ORM 表（否则新建的空库会缺表）。"""
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "migrations")
        script = ScriptDirectory.from_config(cfg)
        # 把全部迁移文件拼起来检查：表可以分批建（0001 初始、0002 加任务表…），
        # 但只要有一个 ORM 表在整个迁移链里都没出现，新库就会缺表。
        src = ""
        for rev in script.walk_revisions():
            src += open(rev.module.__file__, encoding="utf-8").read()

        _register_models()
        for name in Base.metadata.tables:
            assert f"'{name}'" in src or f'"{name}"' in src, f"迁移里缺少表 {name}"
