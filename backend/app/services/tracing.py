"""Langfuse 全链路追踪（11-1，可选组件）。

设计要点（为什么这么做）：
1. **彻底可选**：`LANGFUSE_PUBLIC_KEY/SECRET_KEY` 任一为空时，SDK 根本不初始化，
   所有埋点函数变成零开销 no-op——没部署 Langfuse 的环境零影响、不改代码。
2. **两种挂法**：
   - `open_trace/open_span/open_generation`：手动开、手动 close()。给 SSE 流式
     场景用（生成器每次 yield 都可能换线程，contextvar 跨不可靠，必须显式传节点）。
   - `span()/generation()` 上下文管理器：单个同步函数体内用（同一线程内，
     自动开闭自动计时），如 retrieve() 内部的翻译/向量/BM25/重排。
3. **trace id 用 request_id**：Langfuse 里直接搜 request_id 就能定位一次请求，
   和日志、SSE 错误提示里的 request_id 三方对得上。
4. **永不抛错**：追踪挂了不能影响业务——所有 Langfuse 调用都吞异常只记 warning。

追踪范围（PRD 全链路）：
- chat：合规预检 → 检索（翻译变体/向量/BM25/融合/重排）→ LLM 生成 → 输出合规
- ingest：每篇文档的解析→切块→嵌入→入库
- debug/eval：检索调试台与批量评测
"""
import logging
import random
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

logger = logging.getLogger(__name__)

# 当前 trace 节点（仅单线程同步流程用：trace() CM 进入时挂上，供模块级 span() 取父）
_current: ContextVar = ContextVar("lf_current_node", default=None)


def _safe(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.warning("Langfuse 调用失败（忽略）", exc_info=True)


class _Noop:
    """未启用追踪时的空实现：接口与 _Node 完全一致，调用方无感知。"""

    def open_span(self, name: str, **attrs) -> "_Noop":
        return self

    def open_generation(self, name: str, **attrs) -> "_Noop":
        return self

    @contextmanager
    def span_cm(self, name: str, **attrs):
        yield self

    @contextmanager
    def generation_cm(self, name: str, **attrs):
        yield self

    def update(self, **attrs) -> None:
        pass

    def close(self, output=None, **attrs) -> None:
        pass

    def finish(self, output=None, **attrs) -> None:
        pass


class _Node:
    """Langfuse 节点（trace/span/generation 通用）的薄包装。

    - open_* 返回子节点，调用方负责 close()（适合跨生成器迭代的流式场景）；
    - *_cm 是上下文管理器，自动开闭自动计时（适合单线程同步代码块）。
    """

    def __init__(self, obj, kind: str):
        self._obj = obj
        self._kind = kind
        self._t0 = time.perf_counter()

    # ---- 手动开闭（跨线程/跨迭代安全）----
    def open_span(self, name: str, **attrs) -> "_Node | _Noop":
        try:
            return _Node(self._obj.span(name=name, **attrs), "span")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse span 创建失败（忽略）：%s", exc)
            return _Noop()

    def open_generation(self, name: str, *, model: str | None = None,
                        provider: str | None = None, input=None,
                        metadata: dict | None = None) -> "_Node | _Noop":
        try:
            return _Node(
                self._obj.generation(
                    name=name, model=model, input=input,
                    metadata={"provider": provider, **(metadata or {})},
                ),
                "generation",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse generation 创建失败（忽略）：%s", exc)
            return _Noop()

    # ---- 上下文管理器（单线程同步代码块内用）----
    @contextmanager
    def span_cm(self, name: str, **attrs):
        child = self.open_span(name, **attrs)
        try:
            yield child
            child.close()
        except BaseException as exc:
            child.close(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
            raise

    @contextmanager
    def generation_cm(self, name: str, **attrs):
        child = self.open_generation(name, **attrs)
        try:
            yield child
            child.close()
        except BaseException as exc:
            child.close(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
            raise

    # ---- 公共 ----
    def update(self, **attrs) -> None:
        _safe(self._obj.update, **attrs)

    def close(self, output=None, **attrs) -> None:
        """收尾节点：补 output/计时，然后 end()。

        注意：v2 SDK 里只有 span/generation 有 end()，trace 对象没有
        （trace 在 ingestion 侧自动定稿），所以要按类型区分。
        """
        took_ms = int((time.perf_counter() - self._t0) * 1000)
        payload: dict = {"metadata": {"took_ms": took_ms, **attrs.pop("metadata", {})}}
        if output is not None:
            payload["output"] = output
        payload.update(attrs)
        _safe(self._obj.update, **payload)
        if self._kind != "trace":
            _safe(self._obj.end)

    def finish(self, output=None, **attrs) -> None:
        """收尾整条 trace：补 output/计时 → flush 立即上报（trace 无 end()）。"""
        self.close(output=output, **attrs)
        _safe_flush()


def _safe_flush() -> None:
    client = _client()
    if client is not None:
        _safe(client.flush)


@lru_cache
def _client():
    """Langfuse 客户端单例；未配置 key 返回 None（并缓存住，避免每请求重试）。"""
    from app.config import get_settings

    s = get_settings()
    if not s.langfuse_enabled or not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key,
            host=s.langfuse_host,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse 初始化失败，本次进程追踪降级为 no-op：%s", exc)
        return None


def enabled() -> bool:
    return _client() is not None


def open_trace(name: str, *, trace_id: str | None = None,
               user_id: int | str | None = None, session_id: int | str | None = None,
               input=None, metadata: dict | None = None) -> "_Node | _Noop":
    """开一条 trace（手动模式）。返回 None 型 no-op 或可 close()/finish() 的节点。

    采样：langfuse_sample_rate < 1.0 时按比例丢弃（压测时调低，避免追踪洪峰）。
    """
    client = _client()
    if client is None:
        return _Noop()
    from app.config import get_settings

    if get_settings().langfuse_sample_rate < 1.0 and \
            random.random() > get_settings().langfuse_sample_rate:
        return _Noop()
    try:
        t = client.trace(
            id=trace_id or secrets.token_hex(16),
            name=name,
            user_id=str(user_id) if user_id is not None else None,
            session_id=str(session_id) if session_id is not None else None,
            input=input,
            metadata=metadata or {},
        )
        return _Node(t, "trace")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse trace 创建失败（忽略）：%s", exc)
        return _Noop()


@contextmanager
def trace(name: str, *, trace_id: str | None = None, **kw):
    """trace 的上下文管理器版：单线程同步流程（如 ingest、debug 接口）用。

    进入时把节点挂到 contextvar，体内可直接用模块级 span()/generation() 开子阶段。
    """
    node = open_trace(name, trace_id=trace_id, **kw)
    token = _current.set(node)
    try:
        yield node
        node.finish()
    except BaseException as exc:
        node.close(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _current.reset(token)


@contextmanager
def span(name: str, **attrs):
    """给当前上下文里的 trace 开子阶段；流式/无追踪时 no-op。

    依赖调用方先把 trace 节点挂到上下文：单线程流程里用 trace() CM 自动完成；
    流式 chat 里手动模式不挂 contextvar，因此本函数在 chat 流内是 no-op——
    chat 的各阶段改用节点上的 span_cm 显式挂。
    """
    node = _current.get()
    if node is None:
        yield _Noop()
        return
    with node.span_cm(name, **attrs) as child:
        yield child


@contextmanager
def generation(name: str, **attrs):
    """同 span()，但创建 generation 类型节点（LLM 调用专用）。"""
    node = _current.get()
    if node is None:
        yield _Noop()
        return
    with node.generation_cm(name, **attrs) as child:
        yield child


def update_current(**attrs) -> None:
    node = _current.get()
    if node is not None:
        node.update(**attrs)


def flush() -> None:
    _safe_flush()
