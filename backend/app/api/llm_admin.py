"""LLM 提供商管理 API（管理后台 · 模型配置页）。

权限：admin:users（系统管理员）。普通用户看不到也碰不到这些接口。

设计说明：
- api_key 出参永远是掩码（****last4），更新时不传 key 表示"保持原值"——
  防止前端把掩码当真值写回库里。
- 任何写操作后调 invalidate()，30 秒配置缓存立即失效，下一条消息就用新配置。
- is_primary 全局唯一：activate 接口负责"换主用"，其它接口不碰这个字段。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.db.session import get_db
from app.models.llm_config import LlmProviderConfig
from app.models.user import User
from app.services.llm_config import (
    PROVIDER_PRESETS,
    decrypt_key,
    encrypt_key,
    invalidate,
    key_hint,
    probe_provider,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin/llm-providers", tags=["llm-admin"])


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=32)
    base_url: str = Field(min_length=1, max_length=255)
    model: str = Field(min_length=1, max_length=64)
    # 更新时可留空 = 保持原 Key；新增时必填
    api_key: str = Field(default="", max_length=200)
    remark: str = Field(default="", max_length=255)
    enabled: bool = True


@router.get("/presets")
def list_presets(user: User = Depends(require_permission("admin:users"))) -> dict:
    """前端下拉用的厂商预设（label / 默认 base_url / 常用 model）。"""
    return {"presets": PROVIDER_PRESETS}


@router.get("")
def list_providers(
    user: User = Depends(require_permission("admin:users")),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.query(LlmProviderConfig).order_by(LlmProviderConfig.id.asc()).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "provider": r.provider,
            "base_url": r.base_url,
            "model": r.model,
            "key_hint": key_hint(decrypt_key(r.api_key_enc)),
            "enabled": r.enabled,
            "is_primary": r.is_primary,
            "remark": r.remark,
            "updated_at": r.updated_at.isoformat(),
        }
        for r in rows
    ]


@router.post("")
def create_provider(
    body: ProviderIn,
    user: User = Depends(require_permission("admin:users")),
    db: Session = Depends(get_db),
) -> dict:
    if not body.api_key.strip():
        raise HTTPException(status_code=422, detail={"error_code": "E7001", "message": "新增配置必须填写 API Key"})
    row = LlmProviderConfig(
        name=body.name.strip(),
        provider=body.provider.strip(),
        base_url=body.base_url.strip().rstrip("/"),
        model=body.model.strip(),
        api_key_enc=encrypt_key(body.api_key.strip()),
        enabled=body.enabled,
        remark=body.remark.strip(),
        is_primary=False,
    )
    # 第一条配置自动成为主用，避免"配了但不生效"的困惑
    has_primary = db.query(LlmProviderConfig).filter(LlmProviderConfig.is_primary.is_(True)).first() is not None
    if not has_primary:
        row.is_primary = True
    db.add(row)
    db.commit()
    db.refresh(row)
    invalidate()
    logger.info("新增 LLM 提供商 id=%s provider=%s model=%s by=%s", row.id, row.provider, row.model, user.username)
    return {"id": row.id, "is_primary": row.is_primary}


@router.put("/{provider_id}")
def update_provider(
    provider_id: int,
    body: ProviderIn,
    user: User = Depends(require_permission("admin:users")),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(LlmProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "E7002", "message": "配置不存在"})
    row.name = body.name.strip()
    row.provider = body.provider.strip()
    row.base_url = body.base_url.strip().rstrip("/")
    row.model = body.model.strip()
    row.enabled = body.enabled
    row.remark = body.remark.strip()
    if body.api_key.strip():  # 留空 = 不改 Key（前端回显的是掩码，不能拿掩码当真值）
        row.api_key_enc = encrypt_key(body.api_key.strip())
    db.commit()
    invalidate()
    logger.info("更新 LLM 提供商 id=%s by=%s", provider_id, user.username)
    return {"ok": True}


@router.delete("/{provider_id}")
def delete_provider(
    provider_id: int,
    user: User = Depends(require_permission("admin:users")),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(LlmProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "E7002", "message": "配置不存在"})
    db.delete(row)
    db.commit()
    invalidate()
    logger.info("删除 LLM 提供商 id=%s by=%s", provider_id, user.username)
    return {"ok": True}


class ProbeIn(BaseModel):
    """草稿试连：允许用"还没保存"的表单值先测一遍。

    api_key 留空且给了 provider_id 时，回落到库里已存的 Key——
    编辑场景下前端只回显掩码，用户不重填也能测。
    """
    base_url: str = Field(default="", max_length=255)
    model: str = Field(default="", max_length=64)
    api_key: str = Field(default="", max_length=200)
    provider_id: int | None = None


@router.post("/test")
def test_draft_provider(
    body: ProbeIn,
    user: User = Depends(require_permission("admin:users")),
    db: Session = Depends(get_db),
) -> dict:
    """草稿配置试连（新增/编辑弹窗里的"测试连接"）。"""
    api_key = body.api_key.strip()
    if not api_key and body.provider_id:
        row = db.get(LlmProviderConfig, body.provider_id)
        if row is not None:
            api_key = decrypt_key(row.api_key_enc)
    result = probe_provider(body.base_url, body.model, api_key)
    logger.info("试连 LLM 草稿 base_url=%s model=%s ok=%s by=%s",
                body.base_url, body.model, result.get("ok"), user.username)
    return result


@router.post("/{provider_id}/test")
def test_saved_provider(
    provider_id: int,
    user: User = Depends(require_permission("admin:users")),
    db: Session = Depends(get_db),
) -> dict:
    """已保存配置试连（列表行上的"测试"按钮）。"""
    row = db.get(LlmProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "E7002", "message": "配置不存在"})
    result = probe_provider(row.base_url, row.model, decrypt_key(row.api_key_enc))
    result["id"] = provider_id
    result["name"] = row.name
    logger.info("试连 LLM id=%s ok=%s by=%s", provider_id, result.get("ok"), user.username)
    return result


@router.post("/{provider_id}/activate")
def activate_provider(
    provider_id: int,
    user: User = Depends(require_permission("admin:users")),
    db: Session = Depends(get_db),
) -> dict:
    """设为主用：该条排调用链最前，其余顺次作为故障切换备选。"""
    row = db.get(LlmProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "E7002", "message": "配置不存在"})
    for r in db.query(LlmProviderConfig).filter(LlmProviderConfig.is_primary.is_(True)).all():
        r.is_primary = False
    row.is_primary = True
    row.enabled = True  # 激活即启用：主用禁用是矛盾状态
    db.commit()
    invalidate()
    logger.info("切换主用 LLM 提供商 id=%s provider=%s by=%s", provider_id, row.provider, user.username)
    return {"ok": True, "is_primary": True}


@router.post("/{provider_id}/toggle")
def toggle_provider(
    provider_id: int,
    user: User = Depends(require_permission("admin:users")),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(LlmProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "E7002", "message": "配置不存在"})
    row.enabled = not row.enabled
    if not row.enabled:
        row.is_primary = False  # 禁用的不能继续当主用，调用链自动落到下一条
        _ensure_some_primary(db)
    db.commit()
    invalidate()
    return {"ok": True, "enabled": row.enabled}


def _ensure_some_primary(db: Session) -> None:
    """主用被删/禁后，把第一条启用的补为主用，避免链上没有"排头兵"。"""
    first = db.query(LlmProviderConfig).filter(LlmProviderConfig.enabled.is_(True)).order_by(LlmProviderConfig.id.asc()).first()
    if first is not None:
        first.is_primary = True
