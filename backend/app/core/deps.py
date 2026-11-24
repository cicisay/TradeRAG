"""认证与鉴权依赖（FastAPI Depends 链）。

给新手的说明：
- get_current_user：从请求头解析 JWT → 查库确认用户仍有效 → 返回 User 对象。
  任何"必须登录"的接口加一个参数即可：user: User = Depends(get_current_user)。
- require_permission("xxx")：工厂函数，生成"必须拥有某权限"的校验器。
  用法：Depends(require_permission("kb:manage"))。
  未登录/令牌过期 → 401（E6001）；已登录但没权限 → 403（E6003）。
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.core.permissions import permissions_of_role
from app.db.session import get_db
from app.models.user import User

# tokenUrl 告诉前端"去哪个接口换令牌"（OpenAPI 文档里会显示）
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """解析令牌并校验用户状态。所有业务接口的鉴权入口。"""
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error_code": "E6001", "message": "登录已过期，请重新登录"},
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise cred_exc
    payload = decode_access_token(token)
    if payload is None:
        raise cred_exc

    user = db.get(User, int(payload["sub"]))
    if user is None or user.status != "active":
        # 账号被禁用/删除后，旧令牌立即失效
        raise cred_exc
    return user


def require_permission(permission: str):
    """权限校验依赖工厂：调用方没这个权限就拒绝（403 / E6003）。"""

    def checker(
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        user_role = user.role.code if user.role else ""
        if permission not in permissions_of_role(user_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "E6003", "message": "没有执行该操作的权限"},
            )
        return user

    return checker
