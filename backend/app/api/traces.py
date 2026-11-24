"""管理后台链路追踪（11-3）：把 Langfuse 的 trace 镜像进管理员后台。

为什么是后端代理而不是前端直连 13000：
- Langfuse 的端口没有经过 nginx，且 secret key 绝不能下发到浏览器；
- Langfuse 属于可选栈（compose `profiles: ["obs"]`），没启动时要返回空状态而不是
  500，否则后台一进这个 tab 就报错，看起来像模块坏了。

为什么字段是"投影"而不是透传：observation 的 input/output 可能是几 KB 的 JSON，
一页 50 条能撑到几 MB，前端渲染直接卡死。这里统一裁剪 + 截断。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query

from app.config import get_settings
from app.core.deps import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/traces", tags=["trace"])

_TIMEOUT = httpx.Timeout(8.0, connect=2.5)
_MAX_TEXT = 4000            # 单个 input/output 的字符上限
_LIST_LIMIT_CAP = 100
_QUESTION_MAX = 160


def _base() -> str:
    return get_settings().langfuse_host.rstrip("/")


def _auth() -> tuple[str, str] | None:
    """返回 Basic Auth 凭据；未配置或总开关关闭时返回 None（走空状态）。"""
    s = get_settings()
    if not s.langfuse_enabled or not s.langfuse_public_key or not s.langfuse_secret_key:
        return None
    return s.langfuse_public_key, s.langfuse_secret_key


def _clip(value: Any, limit: int = _MAX_TEXT) -> Any:
    """把可能很大的 input/output 截断，避免一条 trace 就几百 KB。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"…（已截断，原 {len(value)} 字）"
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)[:limit]
    if len(text) <= limit:
        return value
    return text[:limit] + f"…（已截断，原 {len(text)} 字）"


def _question_of(trace: dict) -> str:
    """trace.input 可能是纯字符串，也可能是 messages 数组，统一取成人话。"""
    raw = trace.get("input")
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, dict):
        text = raw.get("query") or raw.get("question") or json.dumps(raw, ensure_ascii=False)
    elif isinstance(raw, list):
        text = ""
        for msg in reversed(raw):
            if isinstance(msg, dict) and msg.get("role") == "user":
                text = str(msg.get("content") or "")
                break
        text = text or json.dumps(raw, ensure_ascii=False)
    else:
        text = str(raw or "")
    text = " ".join(text.split())
    return text[:_QUESTION_MAX] + ("…" if len(text) > _QUESTION_MAX else "")


def _empty(reason: str, enabled: bool) -> dict:
    return {"enabled": enabled, "reachable": False, "items": [], "total": 0, "reason": reason}


def _call(path: str, params: dict | None = None) -> dict:
    """调用 Langfuse 公共 API；异常统一转成可读的中文原因。"""
    creds = _auth()
    if creds is None:
        raise _NotConfigured()
    try:
        with httpx.Client(timeout=_TIMEOUT, auth=creds) as cli:
            resp = cli.get(f"{_base()}{path}", params=params or {})
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException as exc:
        raise _Unreachable("Langfuse 响应超时（服务可能正在启动或负载过高）") from exc
    except httpx.ConnectError as exc:
        raise _Unreachable("连不上 Langfuse，追踪服务未启动（docker compose --profile obs up -d）") from exc
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            raise _Unreachable("Langfuse 拒绝了请求，public/secret key 不匹配") from exc
        raise _Unreachable(f"Langfuse 返回 {code}") from exc
    except ValueError as exc:  # JSON 解析失败
        raise _Unreachable("Langfuse 返回的不是合法 JSON") from exc


class _NotConfigured(Exception):
    pass


class _Unreachable(Exception):
    pass


@router.get("")
def list_traces(
    hours: int = Query(24, ge=1, le=24 * 30, description="回看多少小时"),
    limit: int = Query(50, ge=1, le=_LIST_LIMIT_CAP),
    user=Depends(require_permission("audit:read")),
) -> dict:
    """最近 trace 摘要列表。Langfuse 未配置/未启动时返回 200 + 空列表 + 原因。"""
    if _auth() is None:
        return _empty("未配置 Langfuse（backend/.env 的 LANGFUSE_PUBLIC_KEY / SECRET_KEY 为空）", enabled=False)

    from_ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        payload = _call("/api/public/traces", {"page": 1, "limit": limit, "fromTimestamp": from_ts})
    except _Unreachable as exc:
        logger.warning("读取 Langfuse trace 列表失败：%s", exc)
        return _empty(str(exc), enabled=True)

    items = []
    for tr in payload.get("data") or []:
        md = tr.get("metadata") or {}
        items.append({
            "id": tr.get("id"),
            "name": tr.get("name"),
            "timestamp": tr.get("timestamp"),
            "latency": tr.get("latency"),
            "userId": tr.get("userId"),
            "sessionId": tr.get("sessionId"),
            "branch": md.get("branch"),
            "intent": md.get("intent"),
            "refs": md.get("refs"),
            "took_ms": md.get("took_ms"),
            "question": _question_of(tr),
            "nodes": len(tr.get("observations") or []),
            "level": tr.get("level") or "DEFAULT",
            "scores": len(tr.get("scores") or []),
        })
    return {
        "enabled": True,
        "reachable": True,
        "items": items,
        "total": (payload.get("meta") or {}).get("totalItems", len(items)),
        "hours": hours,
        "reason": "",
    }


@router.get("/{trace_id}")
def get_trace(
    trace_id: str,
    user=Depends(require_permission("audit:read")),
) -> dict:
    """单条 trace 详情：trace 头 + 全部 observation（父子关系由 parentId 表达）。"""
    if _auth() is None:
        return _empty("未配置 Langfuse（backend/.env 的 LANGFUSE_PUBLIC_KEY / SECRET_KEY 为空）", enabled=False)
    try:
        tr = _call(f"/api/public/traces/{trace_id}")
    except _Unreachable as exc:
        return _empty(str(exc), enabled=True)

    observations = []
    for ob in tr.get("observations") or []:
        parent = ob.get("parentObservationId")
        observations.append({
            "id": ob.get("id"),
            "parentId": parent,
            "name": ob.get("name"),
            "type": ob.get("type"),
            "startTime": ob.get("startTime"),
            "endTime": ob.get("endTime"),
            "latency": ob.get("latency"),
            "model": ob.get("model"),
            "level": ob.get("level") or "DEFAULT",
            "usage": ob.get("usage") or None,
            "input": _clip(ob.get("input")),
            "output": _clip(ob.get("output")),
            "metadata": _clip(ob.get("metadata")),
        })

    md = tr.get("metadata") or {}
    return {
        "enabled": True,
        "reachable": True,
        "reason": "",
        "trace": {
            "id": tr.get("id"),
            "name": tr.get("name"),
            "timestamp": tr.get("timestamp"),
            "latency": tr.get("latency"),
            "userId": tr.get("userId"),
            "sessionId": tr.get("sessionId"),
            "branch": md.get("branch"),
            "intent": md.get("intent"),
            "refs": md.get("refs"),
            "took_ms": md.get("took_ms"),
            "totalCost": tr.get("totalCost"),
            "tags": tr.get("tags") or [],
            "input": _clip(tr.get("input")),
            "output": _clip(tr.get("output")),
            "metadata": _clip(md, 1200),
        },
        "observations": observations,
    }
