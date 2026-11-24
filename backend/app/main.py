"""FastAPI 应用入口（对应 PRD 3.1 架构图"应用服务层"）。

M1 步骤2 新增：
- 启动时自动建表 + 种子数据（5 种角色 + 默认管理员）
- 挂载认证路由 /api/v1/auth/*（登录换 JWT、查询当前用户）
- 示例管理端点验证 RBAC（admin:users 权限）
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import FRONTEND_DIR, get_settings
from app.core.deps import require_permission
from app.core.middleware import MetricsMiddleware
from app.db.session import SessionLocal, init_db
from app.db.seed import seed_default_data
from app.db.seed_compliance import seed_compliance_data
from app.db.trade_seed import init_trade_db
from app.services.compliance import invalidate_compliance_cache
from app.services.sql_channel import sync_templates
from app.services.warmup import warmup_models_async
from app.api import (
    auth,
    badcases as badcases_api,
    chat as chat_api,
    compliance as compliance_api,
    debug,
    documents,
    feedback as feedback_api,
    jobs as jobs_api,
    kb as kb_api,
    llm_admin,
    monitor,
    sql as sql_api,
    system as system_api,
    traces as traces_api,
)
settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动时建表 + 种子，关闭时无清理项。"""
    init_db()
    seed_default_data()
    # M4：业务库（SQL 通道只读视图）+ 合规词库/规则 + SQL 模板注册
    init_trade_db()
    db = SessionLocal()
    try:
        seed_compliance_data(db)
        sync_templates(db)
    finally:
        db.close()
    invalidate_compliance_cache()
    logger.info("数据库初始化完成（表结构 + 种子数据 + 合规词库 + SQL 模板）")
    # 步骤8：后台预热真实模型（bge-m3 / 重排 / OCR），避免首个请求等几十秒
    warmup_models_async()
    # 11-3：DB → Prometheus Gauge 桥接线程（30s 刷新业务存量指标）
    monitor.start_metrics_bridge()
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
    description="外贸行业 RAG 知识库问答系统（PRD v1.0）",
    lifespan=lifespan,
)

# CORS：开发阶段放开本地前端端口；生产收敛到具体域名（M6 处理）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 指标中间件（11-2）：放在 CORS 之后 add，成为更外层，CORS 之前的异常也能计数。
# 纯 ASGI 实现，不影响 SSE 流式输出。
app.add_middleware(MetricsMiddleware)

# ---- 路由挂载 ----
# 注意：各 router 已自带 /api/v1/... 前缀，这里不再重复加 prefix（否则双重前缀 404）
app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(jobs_api.router)
app.include_router(kb_api.router)
app.include_router(llm_admin.router)
app.include_router(debug.router)
app.include_router(chat_api.router)
app.include_router(feedback_api.router)
app.include_router(compliance_api.router)
app.include_router(sql_api.router)
app.include_router(system_api.router)
app.include_router(badcases_api.router)
app.include_router(monitor.router)
app.include_router(monitor.metrics_router)  # /metrics（Prometheus 抓取）
app.include_router(traces_api.router)  # 管理后台链路追踪（11-3，Langfuse 只读镜像）


# ---- 静态前端（M5）----
# 同一进程托管，避免前端再起一个开发服务器；API 与前端同源，免 CORS 烦恼。
if settings.frontend_enabled and Path(FRONTEND_DIR).exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/app/")


@app.get("/health", tags=["system"])
def health() -> dict:
    """健康检查：返回应用名与环境，供监控探活。"""
    return {"status": "ok", "app": settings.app_name, "env": settings.app_env}


@app.get("/api/v1/admin/ping", tags=["admin"])
def admin_ping(user: dict = Depends(require_permission("admin:users"))) -> dict:
    """RBAC 验证端点：仅系统管理员可访问（后续用户管理接口在此扩展）。"""
    return {"pong": True, "username": user.username}
