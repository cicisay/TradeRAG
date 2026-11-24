"""HTTP 层指标中间件（11-2）。

为什么用纯 ASGI 而不是 `@app.middleware("http")`：
- BaseHTTPMiddleware 会把响应包一层，SSE 流式场景历史上出现过缓冲/断连问题；
  纯 ASGI 只旁路 send、不改消息，对 `/api/v1/chat` 的流式输出零干扰。
- 只记"模板路由"（`/api/v1/chat`），不记原始 URL：否则扫描器随便打几个
  不存在的路径，标签基数就能把 Prometheus 撑爆。
"""
import time

from app.services import metrics


class MetricsMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        # 抓取端点自身不计入（否则每 15 秒一条，污染 QPS）；预检请求也不计
        if path == "/metrics" or method == "OPTIONS":
            return await self.app(scope, receive, send)

        metrics.inflight(1)
        started = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            metrics.observe_http(method, route, status["code"], time.perf_counter() - started)
            metrics.inflight(-1)
