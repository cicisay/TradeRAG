"""Alembic 迁移环境（对应 PRD 6.1「生产切 PostgreSQL 后改 Alembic 迁移」）。

给新手的说明：
- Alembic 是 SQLAlchemy 官方推荐的"数据库版本管理"工具。作用类似代码的 git：
  每次表结构变更记一个版本文件，部署时按顺序执行，就能把任意环境的库升到最新结构。
- 为什么不继续用 create_all？因为 create_all 只会"建缺失的表"，**不会改已有的表**
  （加列、改类型、加索引都不管）。生产库不能删了重建，必须靠迁移脚本逐步演进。
- 连接串不写在 alembic.ini 里，而是从应用配置（.env 的 DATABASE_URL）读取，
  保证"应用连哪个库，迁移就连哪个库"，避免出现"迁移跑到了 A 库、应用连的是 B 库"。
- 也可以用 -x url=... 临时指定（迁移/演练时很有用）：
      alembic -x url=postgresql+psycopg://rag:rag@127.0.0.1:5432/rag upgrade head
"""
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine, pool

from alembic import context

# 让 alembic 能 import 到 app 包（脚本从 backend/ 目录执行）
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.db.session import Base, _register_models  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 必须先注册模型，metadata 里才有表
_register_models()
target_metadata = Base.metadata


def _database_url() -> str:
    """优先 -x url=...，其次应用配置（.env / 环境变量）。"""
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override
    return get_settings().database_url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库（交付 DBA 审核时用）。"""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _database_url()
    connectable = create_engine(url, poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # SQLite 不支持大部分 ALTER TABLE，batch 模式会用"建新表-搬数据-改名"来模拟
            render_as_batch=url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
