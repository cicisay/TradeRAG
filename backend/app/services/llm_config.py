"""LLM 提供商配置服务：加密存取 + 调用链缓存。

设计要点（给新手）：
- 加密：Fernet 对称加密，密钥从 settings.secret_key 派生（SHA-256）。
  改 secret_key 会使库里所有 Key 无法解密——部署后固定 secret_key，别随意改。
- 调用链：primary 最先 → 其余 enabled 按 id 升序。一个挂了切下一个，
  避免单厂商故障导致整个问答瘫痪（用户核心诉求）。
- 缓存：LLM 配置 30 秒 TTL 缓存，聊天高频路径不反复查库；
  管理员改动后调 invalidate()，立即生效。
- 兜底：数据库一条配置都没有时，回退到 .env 里的 minimax/deepseek/qwen
  （保持老行为不变），所以升级后零配置也能用。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.llm_config import LlmProviderConfig

logger = logging.getLogger(__name__)

_CACHE_TTL = 30.0
_chain_cache: list[dict] | None = None
_chain_cached_at = 0.0


def _fernet() -> Fernet:
    digest = hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_key(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_key(enc: str) -> str:
    try:
        return _fernet().decrypt(enc.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        # secret_key 变过或数据损坏：解不出就用不了，返回空让调用方跳过这个 provider
        logger.error("api_key 解密失败（secret_key 是否改过？），该提供商将被跳过")
        return ""


def key_hint(plain: str) -> str:
    """掩码展示：只露末 4 位。太短的 key 一律打全码。"""
    if len(plain) <= 8:
        return "****"
    return f"****{plain[-4:]}"


@dataclass(frozen=True)
class ProviderEntry:
    """一次可调用的大模型配置。"""
    source: str        # "db" 或 "env"
    ref_id: int | None  # db 配置的 id
    name: str
    provider: str
    base_url: str
    model: str
    api_key: str


def env_chain() -> list[ProviderEntry]:
    """.env 兜底链（数据库无配置时用），等价于改造前的行为。

    顺序（用户指定）：MiniMax 主用，DeepSeek 备用。
    权衡记录：MiniMax-M3.1-Flash 是推理模型，正文前要先吐 reasoning 思考块，
    首字明显慢于 DeepSeek（实测 10-4：DeepSeek 812ms vs MiniMax 十几秒）；
    但 MiniMax 回答质量/风格更符合当前使用习惯，且 429/故障会自动切
    DeepSeek（见 llm.py 的分级退避），所以主用 MiniMax、DeepSeek 兜底。
    """
    s = get_settings()
    entries = []
    for name, key, base_url, model, provider in (
        ("环境变量·MiniMax", s.minimax_api_key, s.minimax_base_url, s.minimax_model, "minimax"),
        ("环境变量·DeepSeek", s.deepseek_api_key, s.deepseek_base_url, s.deepseek_model, "deepseek"),
        ("环境变量·Qwen", s.qwen_api_key, s.qwen_base_url, s.qwen_model, "qwen"),
    ):
        if key:
            entries.append(ProviderEntry("env", None, name, provider, base_url, model, key))
    return entries


def invalidate() -> None:
    """管理员改动配置后调用，让新配置立即生效。"""
    global _chain_cache
    _chain_cache = None


def get_provider_chain(db: Session) -> list[ProviderEntry]:
    """按优先级返回当前可用的提供商列表：primary 在前，其余 enabled 在后。

    primary 失效/禁用不影响其它条目——它只是"排最前"的偏好，不是硬依赖。
    """
    global _chain_cache, _chain_cached_at
    if _chain_cache is not None and time.time() - _chain_cached_at < _CACHE_TTL:
        return _chain_cache

    entries: list[ProviderEntry] = []
    rows = db.query(LlmProviderConfig).order_by(LlmProviderConfig.id.asc()).all()
    primary = next((r for r in rows if r.is_primary and r.enabled), None)
    ordered = ([primary] if primary else []) + [r for r in rows if r is not primary]
    for r in ordered:
        if not r.enabled:
            continue
        key = decrypt_key(r.api_key_enc)
        if not key:
            continue  # 解不开的（如换过 secret_key）跳过，不让单条脏数据拖垮整条链
        entries.append(ProviderEntry("db", r.id, r.name, r.provider, r.base_url, r.model, key))

    if not entries:
        entries = env_chain()

    _chain_cache = entries
    _chain_cached_at = time.time()
    return entries


# 提供商预设：前端新增/编辑时给出合理的默认值，减少填错 base_url 的来回试错
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "minimax": {"label": "MiniMax", "base_url": "https://api.minimaxi.com/v1", "model": "MiniMax-M3.1-Flash"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "qwen": {"label": "阿里云 Qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "moonshot": {"label": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "zhipu": {"label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-air"},
    "ollama": {"label": "Ollama（本机）", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b"},
    "custom": {"label": "自定义（OpenAI 兼容）", "base_url": "", "model": ""},
}


def probe_provider(base_url: str, model: str, api_key: str, timeout: float = 20.0) -> dict:
    """真发一次最小对话请求，验证 base_url / model / api_key 三者是否真能用。

    为什么不用 GET /models 探活：
    - 不少网关没实现该接口，但对话是好的；
    - 反过来它验不出模型是否真的可调用——MiniMax-M2 就是"列表里有、调用报 2069 已下线"。
    只有打一次 /chat/completions 才算数。
    """
    base = (base_url or "").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"ok": False, "error_code": "E7101", "message": "Base URL 必须以 http:// 或 https:// 开头"}
    if not (model or "").strip():
        return {"ok": False, "error_code": "E7102", "message": "模型名不能为空"}
    if not api_key:
        return {"ok": False, "error_code": "E7103", "message": "没有可用的 API Key（若刚改过 secret_key，请重新录入 Key）"}

    payload = {
        "model": model.strip(),
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "stream": False,
    }
    started = time.perf_counter()
    try:
        # trust_env=False：不读系统代理。管理面板填的地址（尤其 127.0.0.1 的 Ollama）
        # 常被系统代理劫持出假 502，试连必须直连才能反映真实连通性。
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=8.0), follow_redirects=False, trust_env=False) as client:
            resp = client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
    except httpx.ConnectError:
        return {"ok": False, "error_code": "E7111",
                "message": "连不上该地址：检查 Base URL 填写、本机网络或代理是否拦截"}
    except httpx.TimeoutException:
        return {"ok": False, "error_code": "E7112",
                "message": f"请求超时（>{timeout:.0f}s）：地址可达但服务没响应"}
    except Exception as exc:  # noqa: BLE001  探活不该把异常抛给前端
        return {"ok": False, "error_code": "E7119", "message": f"请求异常：{type(exc).__name__}"}

    latency = int((time.perf_counter() - started) * 1000)
    if resp.status_code == 200:
        try:
            data = resp.json()
            content = data["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError):
            return {"ok": False, "error_code": "E7113", "status": 200,
                    "message": "返回 200 但结构不是 OpenAI 兼容格式（缺少 choices[0].message）"}
        return {"ok": True, "latency_ms": latency, "model": model.strip(), "reply": content.strip()[:40]}

    # 非 200 按状态码给可执行的处置建议，而不是甩一串英文
    hints = {
        401: ("E7121", "Key 无效或已过期（401）"),
        402: ("E7124", "余额不足（402）"),
        403: ("E7122", "Key 无该模型权限（403）"),
        404: ("E7123", "接口或模型不存在（404）：检查 Base URL 是否缺 /v1、模型名是否拼错"),
        429: ("E7124", "触发限流或额度用尽（429）"),
    }
    code, message = hints.get(resp.status_code, ("E7199", f"厂商返回 {resp.status_code}"))
    if resp.status_code >= 500:
        message = f"厂商服务异常（{resp.status_code}），稍后重试或切换备用"
    detail = ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        detail = str(err.get("message") or body.get("base_resp", {}).get("status_msg") or "")
    except ValueError:
        detail = resp.text
    message += f"：{detail[:160]}" if detail else ""
    return {"ok": False, "error_code": code, "status": resp.status_code, "message": message}
