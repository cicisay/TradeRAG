"""数据库连接与会话管理（对应 PRD 6.1：SQLite → PostgreSQL）。

给新手的说明：
- SQLAlchemy 是 Python 最常用的 ORM：用 Python 类代替手写 SQL 建表/查表。
- engine 是"数据库连接池"，SessionLocal 是"每次请求借一个会话、用完归还"的工厂。
- get_db 是 FastAPI 依赖注入：接口声明 db: Session = Depends(get_db)，
  请求结束自动关闭，不会泄漏。
- 切换数据库只改 .env 的 DATABASE_URL，代码零改动：
    sqlite:///./data/app.db                          （本地开发，零依赖）
    postgresql+psycopg://rag:密码@127.0.0.1:5432/rag （预发/生产）

M6 切换 PostgreSQL 后必须知道的四个差异：
1. **连接池**：SQLite 是本地文件，开连接几乎无成本；PG 是网络服务，建连要几十毫秒。
   不配池，并发一上来就会"连接风暴"，把 PG 的 max_connections 打满。
2. **pool_pre_ping**：PG 连接会被防火墙 / 服务端空闲超时静默掐断。借出前先探活，
   失败自动换一条，否则用户会看到"第一次请求必报 server closed the connection"。
3. **statement_timeout**：连接级语句超时兜底，防慢查询占死连接池
   （与 SQL 五铁律的 3 秒超时是两层保护，这里是最后一道）。
4. **外键约束**：SQLite 默认**不检查**外键（历史遗留），PG 默认强制检查。
   这里对 SQLite 显式打开 foreign_keys，让本地与生产行为一致——否则会出现
   "本地测试全过、上线第一笔写入就报外键不存在"。
"""
import logging
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import BASE_DIR, get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


# ---------------------------------------------------------------- 后端判定
def is_sqlite(url: str | None = None) -> bool:
    return (url or settings.database_url).startswith("sqlite")


def is_postgres(url: str | None = None) -> bool:
    url = url or settings.database_url
    return url.startswith("postgresql") or url.startswith("postgres://")


def db_backend(url: str | None = None) -> str:
    url = url or settings.database_url
    if is_sqlite(url):
        return "sqlite"
    if is_postgres(url):
        return "postgresql"
    return "other"


# ---------------------------------------------------------------- engine
def _build_engine() -> Engine:
    url = settings.database_url

    if is_sqlite(url):
        eng = create_engine(
            url,
            # FastAPI 用线程池跑同步接口，连接会跨线程使用
            connect_args={"check_same_thread": False, "timeout": settings.db_busy_timeout_s},
            echo=False,
            future=True,
        )

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")          # 与 PG 对齐约束行为
            cur.execute("PRAGMA journal_mode=WAL")         # 读写并发更好
            cur.execute(f"PRAGMA busy_timeout={settings.db_busy_timeout_s * 1000}")
            cur.close()

        return eng

    # ---- PostgreSQL ----
    connect_args: dict = {
        "application_name": settings.app_name,
        # 会话固定 UTC；语句超时兜底。psycopg2/psycopg3 都认 options
        "options": f"-c timezone=UTC -c statement_timeout={settings.db_statement_timeout_ms}",
    }
    if "+asyncpg" in url:
        connect_args = {}  # 本项目用同步驱动，正常不会走到
    return create_engine(
        url,
        echo=False,
        future=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=settings.db_pool_pre_ping,
        connect_args=connect_args,
    )


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：提供一个请求级数据库会话，用完自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------- 建表
def _register_models() -> None:
    """确保所有表类注册到 Base.metadata，否则 create_all 不会建表。"""
    import app.models.user  # noqa: F401
    import app.models.knowledge_base  # noqa: F401
    import app.models.document  # noqa: F401
    import app.models.chunk  # noqa: F401
    import app.models.chat  # noqa: F401
    import app.models.compliance  # noqa: F401
    import app.models.sql_channel  # noqa: F401
    import app.models.ops  # noqa: F401  M5：反馈 / badcase / 评测记录
    import app.models.jobs  # noqa: F401  M6：异步摄入任务台账
    import app.models.llm_config  # noqa: F401  M6+：多模型提供商配置


def init_db() -> None:
    """建表（开发/预发 create_all；生产走 Alembic）。

    - SQLite：create_all 只建缺失的表，不会给老表加列，因此额外做幂等补列。
    - PostgreSQL：设 DB_AUTO_CREATE=false 后完全交给 `alembic upgrade head`，
      避免应用启动时隐式改生产结构（这是运维事故的常见来源）。
    """
    _register_models()
    if is_postgres() and not settings.db_auto_create:
        logger.info("DB_AUTO_CREATE=false，跳过建表（由 alembic upgrade head 负责）")
        return
    Base.metadata.create_all(bind=engine)
    _ensure_added_columns()


# 开发库增量列（表名, 列名, 列定义）——仅 SQLite 历史库需要；PG 交给 Alembic
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("chunk", "page_no", "INTEGER"),
    ("doc_version", "ocr_used", "BOOLEAN DEFAULT 0"),
    ("doc_version", "ocr_confidence", "FLOAT"),
    ("doc_version", "ocr_low_pages", "JSON DEFAULT '[]'"),
    ("doc_version", "ocr_error", "TEXT"),
    ("message", "retrieval_meta_json", "TEXT"),
    ("message", "feedback", "VARCHAR(8)"),
    ("message", "feedback_reason", "VARCHAR(32)"),
]


def _ensure_added_columns() -> None:
    if not is_sqlite():
        return  # 生产走 Alembic 迁移
    with engine.connect() as conn:
        for table, column, ddl in _ADDED_COLUMNS:
            existing = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            }
            if not existing or column in existing:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            conn.commit()


# ---------------------------------------------------------------- 自检
def db_info() -> dict:
    """数据库自检信息（供 /api/v1/system/db 与 scripts/check_db.py 使用）。"""
    info: dict = {
        "backend": db_backend(),
        "url": _mask_url(settings.database_url),
        "driver": engine.dialect.driver,
        "statement_timeout_ms": settings.db_statement_timeout_ms,
    }
    try:
        with engine.connect() as conn:
            if is_postgres():
                row = conn.execute(text(
                    "select version(), current_setting('TimeZone'), current_database(), "
                    "current_user, pg_size_pretty(pg_database_size(current_database()))"
                )).fetchone()
                info.update({
                    "version": (row[0] or "").split(" on ")[0],
                    "server_timezone": row[1],
                    "database": row[2],
                    "db_user": row[3],
                    "size": row[4],
                })
                info["tables"] = conn.execute(text(
                    "select count(*) from information_schema.tables "
                    "where table_schema = current_schema() and table_type='BASE TABLE'"
                )).scalar()
                info["alembic_revision"] = _alembic_revision(conn)
            else:
                info.update({
                    "version": conn.execute(text("select sqlite_version()")).scalar(),
                    "server_timezone": "UTC(应用层统一)",
                    "database": resolve_sqlite_path(settings.database_url),
                    "journal_mode": conn.execute(text("PRAGMA journal_mode")).scalar(),
                    "foreign_keys": conn.execute(text("PRAGMA foreign_keys")).scalar(),
                })
                info["tables"] = conn.execute(text(
                    "select count(*) from sqlite_master "
                    "where type='table' and name not like 'sqlite_%'"
                )).scalar()
            info["ok"] = True
    except Exception as exc:  # noqa: BLE001
        info.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        return info

    pool = engine.pool
    if hasattr(pool, "size"):
        info["pool"] = {
            "class": type(pool).__name__,
            "size": pool.size(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
        }
    return info


def _alembic_revision(conn) -> str | None:  # noqa: ANN001
    try:
        return conn.execute(text("select version_num from alembic_version")).scalar()
    except Exception:  # noqa: BLE001  表还不存在
        return None


def _mask_url(url: str) -> str:
    """日志/接口里不回显密码。"""
    if "@" not in url or "//" not in url:
        return url
    scheme, rest = url.split("//", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        return f"{scheme}//{creds.split(':', 1)[0]}:***@{host}"
    return url


def resolve_sqlite_path(url: str) -> str:
    """把 sqlite:///xxx 解析成绝对路径（迁移脚本用）。"""
    raw = url.removeprefix("sqlite:///")
    p = Path(raw)
    return str(p if p.is_absolute() else BASE_DIR / p)
