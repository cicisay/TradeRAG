"""密码哈希与 JWT 令牌（对应 PRD 6.1 password_hash、E6xxx 鉴权错误码）。

给新手的说明：
- 密码绝不存明文：存 bcrypt 哈希。登录时把用户输入的密码"再哈希一次"比对，而不是解密。
- JWT（JSON Web Token）像一张"临时通行证"：登录成功后发给前端，
  前端每次请求带上它（放在 Authorization 头），服务端验签后知道"你是谁"。
- 通行证有过期时间（默认 480 分钟 = 8 小时，对应一个工作日），过期必须重新登录（E6xxx）。
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

settings = get_settings()

# bcrypt：目前业界公认安全的密码哈希算法，自带"加盐"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# PRD 5.8.2 E6xxx：鉴权过期 → "登录已过期，请重新登录"
JWT_ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: int, username: str, role_code: str, biz_group: str) -> str:
    """签发通行证。载荷里带身份四要素，后续检索隔离直接用，避免每次查库。"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),          # 标准字段：用户唯一标识
        "username": username,
        "role": role_code,
        "biz_group": biz_group,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """验签 + 取载荷。无效/过期返回 None（上层转 401）。"""
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
