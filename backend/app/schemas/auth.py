"""认证相关 Pydantic 模型（出入参格式定义，对应 PRD 9 API 设计）。

Pydantic = 出入参的"质检员"：类型不对/字段缺失直接 422 拒绝，业务代码不用手写校验。
"""
from typing import Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=64)
    # 图形验证码（防自动化爆破）。开关关闭时后端不校验，字段可缺省——
    # 这样旧的脚本/测试不传也能跑，开着开关的正式环境则必须传。
    captcha_id: str | None = Field(default=None, max_length=64)
    captcha_text: str | None = Field(default=None, max_length=8)
    # 登录入口：user=用户登录 tab，admin=管理员登录 tab。
    # 缺省（None）表示不校验入口，供 API 直连/自动化脚本使用；
    # 网页登录必须显式传，否则后台账号可从普通用户入口进入。
    role: Literal["user", "admin"] | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 有效期秒数，前端据此提前刷新


class UserOut(BaseModel):
    """用户信息出参：绝不输出 password_hash。"""

    id: int
    username: str
    role: str
    role_name: str
    biz_group: str
    permissions: list[str]

    class Config:
        from_attributes = True  # 允许从 ORM 对象直接转换
