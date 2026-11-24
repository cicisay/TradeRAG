"""认证 API（对应 PRD 9：登录换令牌 / 获取当前用户）。

- POST /api/v1/auth/login  用户名+密码 → JWT 令牌
- GET  /api/v1/auth/me     带令牌 → 当前用户身份与权限（前端据此渲染菜单）
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.core.permissions import ROLE_NAMES, permissions_of_role, portal_of_role
from app.core.security import create_access_token, verify_password
from app.config import get_settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, TokenResponse, UserOut
from app.services.captcha import new_captcha, verify_captcha

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.get("/captcha")
def get_captcha() -> dict:
    """取一张图形验证码。返回 captcha_id 和图片 data URI，登录时一并提交。

    允许未登录访问——它本来就是登录前要拿的。频率靠 captcha 服务的内存上限兜底。
    APP_ENV=dev 时额外返回 debug_answer：本地联调/自动化冒烟能拿到答案；
    生产（prod）绝不回显答案。
    """
    captcha_id, image, answer = new_captcha()
    out = {"captcha_id": captcha_id, "image": image}
    if get_settings().app_env == "dev":
        out["debug_answer"] = answer
    return out


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """登录。

    安全要点：
    1. 先验验证码再查用户，自动化脚本连"账号是否存在"都探测不到；
    2. 无论"用户不存在"还是"密码错"，都返回同一句提示，
       防止攻击者靠报错差异探测系统里有哪些用户名（用户枚举攻击）。
    验证码不区分大小写；校验失败后该验证码立即作废，需换新图重试。
    """
    if not verify_captcha(body.captcha_id, body.captcha_text):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "E6005", "message": "验证码错误或已过期，请点击图片刷新后重试"},
        )
    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "E6001", "message": "用户名或密码错误"},
        )
    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": "E6003", "message": "账号已停用，请联系管理员"},
        )

    # 登录入口校验：密码已通过说明是本人在操作，此时才提示入口错误不构成账号探测。
    # 后台角色（管理员/审计员/合规/知识管理员）不得从「用户登录」入口进入，反之亦然。
    if body.role is not None:
        actual_portal = portal_of_role(user.role.code)
        if actual_portal != body.role:
            want = "「管理员登录」入口" if actual_portal == "admin" else "「用户登录」入口"
            logger.warning(
                "登录入口不匹配 username=%s role=%s 请求入口=%s",
                body.username, user.role.code, body.role,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "E6006",
                    "message": f"该账号属于{ROLE_NAMES.get(user.role.code, '其他')}，请使用{want}登录",
                    "portal": actual_portal,
                },
            )

    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role_code=user.role.code,
        biz_group=user.biz_group,
    )
    logger.info("用户登录成功 username=%s", body.username)
    return TokenResponse(
        access_token=token,
        expires_in=get_settings().access_token_expire_minutes * 60,
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    """当前登录用户信息：角色 + 权限列表（前端菜单显隐依据）。"""
    role_code = user.role.code if user.role else ""
    return UserOut(
        id=user.id,
        username=user.username,
        role=role_code,
        role_name=ROLE_NAMES.get(role_code, role_code),
        biz_group=user.biz_group,
        permissions=sorted(permissions_of_role(role_code)),
    )
