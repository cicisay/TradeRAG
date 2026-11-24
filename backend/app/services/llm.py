"""LLM 客户端：多提供商调用链 + 自动故障切换（对应「防单厂商故障瘫痪」需求）。

给新手的说明：
- 大模型服务都提供相似的 HTTP 接口（OpenAI 兼容协议）：POST /chat/completions。
- 调用链从数据库读（管理后台维护），primary 在前；一条挂了自动切下一条，
  全部挂了才向上抛错。数据库没有任何配置时回退 .env（minimax/deepseek/qwen）。
- 流式重试有个讲究：如果第一个厂商已经吐了一半字才断，此时换厂商重试会造成
  "答案前后来自两个模型"的拼接事故——所以只在**一个字都没吐**时切换，
  吐到一半失败就直接报错交给上层兜底话术。
- 流式（stream=True）：服务端一段段吐文字，前端"打字机"显示，首字延迟低（FR-209）。
- MiniMax M3/M2.7 等会把 <think> 推理块写进正文，用 _ThinkStripper 剥掉，
  否则用户会看到模型的"内心独白"。
"""
import json
import logging
import re
import time
from collections.abc import Callable, Iterator

import httpx
from sqlalchemy.orm import Session

from app.services import metrics

logger = logging.getLogger(__name__)

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"

_RETRYABLE_HINT = "所有配置的大模型均不可用"

# 单家提供商最多尝试次数（首次 + 1 次重试）。重试只针对"等一下可能就好"的错误。
_MAX_ATTEMPTS = 2
# 5xx/超时的退避时长（秒）：厂商抖动时给一次喘息机会，不无限重试。
_BACKOFF_SECONDS = 0.6
# 瞬时故障：值得原地重试。
_TRANSIENT_STATUS = frozenset({500, 502, 503, 504})

# 不支持 stream_options 的厂商（按 base_url 记）。命中后不再重复试探，
# 直接把该家降级为"无 usage 模式"，token 统计记到 llm_usage_missing。
_NO_USAGE_HOSTS: set[str] = set()


def _http_error(resp: httpx.Response) -> httpx.HTTPStatusError:
    """构造带响应体的错误。

    默认的 raise_for_status() 丢掉了 body，而 400 的 body 才是判断
    "是不是不支持 stream_options" 的唯一依据，所以这里手动读一次。
    """
    body = ""
    try:
        body = resp.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 - 读不到 body 不影响抛错
        pass
    return httpx.HTTPStatusError(
        f"{resp.status_code} {body[:300]}", request=resp.request, response=resp,
    )


def _is_rate_limited(exc: Exception) -> bool:
    """429 限流：重试只会加剧限流，正确动作是立刻换下一家提供商。"""
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def _is_transient(exc: Exception) -> bool:
    """5xx / 连接超时：等一小会儿重试有意义。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _partial_tail(buf: str, tag: str) -> int:
    """buf 末尾若正好是 tag 的前缀（如 "<thi"），返回该前缀长度，用于跨 chunk 拼接。"""
    for n in range(min(len(tag) - 1, len(buf)), 0, -1):
        if buf.endswith(tag[:n]):
            return n
    return 0


class _ThinkStripper:
    """剥离 <think>...</think> 推理块（MiniMax M3/M2.7 等会输出）。

    为什么要状态机而不是字符串 replace：流式下标签会被切到两个 chunk
    （"<thi" + "nk>"），必须把可能的半个标签扣住，等下一段拼完再判断，
    否则漏出去的就是给用户看的半截思维链。
    """

    def __init__(self) -> None:
        self.buf = ""
        self.in_think = False

    def feed(self, delta: str) -> str:
        self.buf += delta
        out: list[str] = []
        while self.buf:
            if self.in_think:
                idx = self.buf.find(_THINK_CLOSE)
                if idx == -1:
                    keep = _partial_tail(self.buf, _THINK_CLOSE)
                    self.buf = self.buf[-keep:] if keep else ""
                    return "".join(out)
                self.buf = self.buf[idx + len(_THINK_CLOSE):]
                self.in_think = False
            else:
                idx = self.buf.find(_THINK_OPEN)
                if idx == -1:
                    keep = _partial_tail(self.buf, _THINK_OPEN)
                    if keep:
                        out.append(self.buf[:-keep])
                        self.buf = self.buf[-keep:]
                    else:
                        out.append(self.buf)
                        self.buf = ""
                    return "".join(out)
                out.append(self.buf[:idx])
                self.buf = self.buf[idx + len(_THINK_OPEN):]
                self.in_think = True
        return "".join(out)

    def flush(self) -> str:
        rest, self.buf = self.buf, ""
        return "" if self.in_think else rest


def _strip_think(text: str) -> str:
    """非流式版本：去掉推理块；未闭合的（被 max_tokens 截断）从 <think> 处整体丢弃。"""
    if _THINK_OPEN not in text:
        return text
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    idx = cleaned.find(_THINK_OPEN)
    if idx != -1:
        cleaned = cleaned[:idx]
    return cleaned.strip()


class LLMClient:
    """多提供商客户端。链配置见 app/services/llm_config.py。"""

    def __init__(self) -> None:
        # 复用连接池：每次问答重新 TCP+TLS 握手要多花 0.5~1s，直接吃掉首字预算。
        # httpx.Client 是线程安全的，FastAPI 的同步生成器跑在线程池里也能共享。
        self._client = httpx.Client()
        self.chain: list = []  # list[ProviderEntry]

    def close(self) -> None:
        self._client.close()

    def resolve(self, db: Session | None = None) -> None:
        """读取当前调用链。db=None 时只用 .env 兜底链（无数据库上下文的场景）。"""
        from app.services.llm_config import env_chain, get_provider_chain

        self.chain = get_provider_chain(db) if db is not None else env_chain()

    @property
    def available(self) -> bool:
        return bool(self.chain)

    @property
    def provider(self) -> str:
        """当前主用厂商名（仅用于展示/埋点，不代表实际调用成功）。"""
        return self.chain[0].provider if self.chain else "none"

    @property
    def primary_model(self) -> str:
        return self.chain[0].model if self.chain else ""

    def _report_usage(self, usage: dict, entry, mode: str, on_usage: Callable | None) -> None:
        """统一上报用量：Prometheus 计数 + 调用方回调（给 Langfuse 用）。"""
        metrics.record_llm_usage(usage, provider=entry.provider, model=entry.model, mode=mode)
        if on_usage is not None:
            try:
                on_usage(usage, entry.provider, entry.model)
            except Exception:  # noqa: BLE001 - 回调出错不能打断生成
                logger.debug("usage 回调异常", exc_info=True)

    def _stream_raw(
        self, entry, messages: list[dict], temperature: float, max_tokens: int,
        include_usage: bool, on_usage: Callable | None,
    ) -> Iterator[str]:
        payload = {
            "model": entry.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if include_usage:
            # OpenAI 兼容协议：末帧带 usage。不开启时厂商一律不返回 token 数。
            payload["stream_options"] = {"include_usage": True}
        with self._client.stream(
            "POST",
            f"{entry.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {entry.api_key}"},
            json=payload,
            timeout=httpx.Timeout(60.0, connect=10.0),
        ) as resp:
            if resp.status_code >= 400:
                raise _http_error(resp)
            stripper = _ThinkStripper()
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[len("data: "):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue  # 心跳/非 JSON 帧
                usage = chunk.get("usage")
                if usage:
                    self._report_usage(usage, entry, "stream", on_usage)
                choices = chunk.get("choices") or []
                if not choices:
                    continue  # usage 帧的 choices 是空数组
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    clean = stripper.feed(delta)
                    if clean:
                        yield clean
            tail = stripper.flush()
            if tail:
                yield tail

    def _attempt_stream(
        self, entry, messages: list[dict], temperature: float, max_tokens: int,
        on_usage: Callable | None = None,
    ) -> Iterator[str]:
        if entry.base_url in _NO_USAGE_HOSTS:
            yield from self._stream_raw(
                entry, messages, temperature, max_tokens, False, on_usage,
            )
            return
        try:
            yield from self._stream_raw(
                entry, messages, temperature, max_tokens, True, on_usage,
            )
        except httpx.HTTPStatusError as exc:
            detail = str(exc).lower()
            # 少数厂商不认 stream_options，会回 400/422。此时一个字都没吐，
            # 去掉该字段重来一次是安全的（不会出现两个模型拼接）。
            if exc.response.status_code in (400, 422) and (
                "stream_options" in detail or "include_usage" in detail
            ):
                _NO_USAGE_HOSTS.add(entry.base_url)
                logger.warning(
                    "厂商 %s 不支持 stream_options，降级为无 usage 模式（token 统计缺失）",
                    entry.provider,
                )
                yield from self._stream_raw(
                    entry, messages, temperature, max_tokens, False, on_usage,
                )
            else:
                raise

    def stream_chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int = 1200,
        db: Session | None = None,
        on_usage: Callable[[dict, str, str], None] | None = None,
    ) -> Iterator[str]:
        """流式对话：逐个 yield 文本增量；厂商失败自动切下一个（见类文档的取舍）。

        on_usage(usage, provider, model) 在收到厂商 usage 帧时回调一次，
        用于把 token 数写进 Langfuse generation（Prometheus 计数由内部完成）。
        """
        self.resolve(db)
        if not self.chain:
            raise RuntimeError("未配置 LLM API Key")
        last_err: Exception | None = None
        for entry in self.chain:
            produced = False
            entry_err: Exception | None = None
            for attempt in range(_MAX_ATTEMPTS):
                if attempt:
                    time.sleep(_BACKOFF_SECONDS)
                try:
                    for delta in self._attempt_stream(
                        entry, messages, temperature, max_tokens, on_usage,
                    ):
                        produced = True
                        yield delta
                    return  # 完整走完，成功
                except Exception as exc:  # noqa: BLE001
                    entry_err = exc
                    # 已经吐字：绝不能换厂商重来，否则用户会看到两个模型的答案拼接
                    if produced:
                        metrics.record_llm_error(
                            provider=entry.provider, model=entry.model, mode="stream",
                        )
                        raise
                    # 429 限流重试只会更糟 → 立刻换下家；401/400 等也换；
                    # 只有 5xx/超时值得原地再来一次
                    if _is_rate_limited(exc) or not _is_transient(exc):
                        break
                    logger.warning(
                        "LLM 提供商 %s(%s) 瞬时失败：%s，%.1fs 后重试",
                        entry.provider, entry.model, exc, _BACKOFF_SECONDS,
                    )
            last_err = entry_err
            if entry_err is not None:
                metrics.record_llm_error(
                    provider=entry.provider, model=entry.model, mode="stream",
                )
            logger.warning(
                "LLM 提供商 %s(%s) 流式失败：%s —— 切换下一个提供商",
                entry.provider, entry.model, entry_err,
            )
        raise last_err or RuntimeError(_RETRYABLE_HINT)

    def chat_once(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 400,
        db: Session | None = None,
    ) -> str:
        """非流式单次调用：合规语义兜底 / 翻译这类"要一个短结果"的场景。

        失败自动切下一个提供商；全挂才抛异常，由调用方决定降级策略
        （合规层选择 fail-open + 记录日志，不该因模型抖动把正常问答全部拒掉）。
        """
        self.resolve(db)
        if not self.chain:
            raise RuntimeError("未配置 LLM API Key")
        last_err: Exception | None = None
        for entry in self.chain:
            entry_err: Exception | None = None
            for attempt in range(_MAX_ATTEMPTS):
                if attempt:
                    time.sleep(_BACKOFF_SECONDS)
                try:
                    resp = self._client.post(
                        f"{entry.base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {entry.api_key}"},
                        json={
                            "model": entry.model,
                            "messages": messages,
                            "temperature": temperature,
                            "max_tokens": max_tokens,
                            "stream": False,
                        },
                        timeout=httpx.Timeout(20.0, connect=8.0),
                    )
                    if resp.status_code >= 400:
                        raise _http_error(resp)
                    data = resp.json()
                    self._report_usage(data.get("usage") or {}, entry, "once", None)
                    return _strip_think(data["choices"][0]["message"].get("content") or "")
                except Exception as exc:  # noqa: BLE001
                    entry_err = exc
                    if _is_rate_limited(exc) or not _is_transient(exc):
                        break
                    logger.warning(
                        "LLM 提供商 %s(%s) 瞬时失败：%s，%.1fs 后重试",
                        entry.provider, entry.model, exc, _BACKOFF_SECONDS,
                    )
            last_err = entry_err
            if entry_err is not None:
                metrics.record_llm_error(
                    provider=entry.provider, model=entry.model, mode="once",
                )
            logger.warning("LLM 提供商 %s(%s) 非流式失败：%s，切换下一个", entry.provider, entry.model, entry_err)
        raise last_err or RuntimeError(_RETRYABLE_HINT)


_client: LLMClient | None = None


def get_llm_client(db: Session | None = None) -> LLMClient:
    """取客户端单例，并按当前配置刷新调用链（配置服务内部有 30s 缓存，开销可忽略）。"""
    global _client
    if _client is None:
        _client = LLMClient()
    _client.resolve(db)
    return _client


def reset_llm_client() -> None:
    """重建客户端（配置变更多用于测试）。顺手关掉旧连接池，避免句柄泄漏。"""
    global _client
    if _client is not None:
        _client.close()
    _client = None
