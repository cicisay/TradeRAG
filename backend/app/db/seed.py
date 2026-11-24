"""种子数据：初始化 5 种角色 + 默认管理员（PRD FR-601）。

用法（两种）：
1. 应用启动时自动执行（lifespan 调 seed_default_data，幂等：已存在就跳过）
2. 手动执行：python -m app.db.seed

默认管理员：admin / admin123（首次登录后应立即改密——改密功能后续步骤提供）。
"""
import logging

from sqlalchemy.orm import Session

from app.core.permissions import ROLE_NAMES, ROLE_ADMIN
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.user import Role, User

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"


def seed_default_data(db: Session | None = None) -> None:
    """幂等初始化：角色缺哪个建哪个；无任何用户时创建默认管理员。"""
    own_session = db is None
    if db is None:
        db = SessionLocal()
    try:
        # 1) 角色
        for code, name in ROLE_NAMES.items():
            if not db.query(Role).filter(Role.code == code).first():
                db.add(Role(code=code, name=name))
        db.commit()

        # 2) 默认管理员（仅当系统里还没有任何用户）
        if not db.query(User).first():
            admin_role = db.query(Role).filter(Role.code == ROLE_ADMIN).first()
            db.add(User(
                username=DEFAULT_ADMIN_USERNAME,
                password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
                role_id=admin_role.id,
                biz_group="default",
            ))
            db.commit()
            logger.info("已创建默认管理员 %s（请首次登录后修改密码）", DEFAULT_ADMIN_USERNAME)
    finally:
        if own_session:
            db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_default_data()
    print("种子数据初始化完成。")
