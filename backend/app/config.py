"""全局配置管理（对应 PRD 3.2 技术选型 / 10 部署方案）。

说明（给新手）：
- 这里用 pydantic-settings 读取 .env 环境变量，好处是：
  1) 类型自动校验（写错类型启动时就报错，而不是运行中悄悄出错）；
  2) 所有配置集中在 Settings 一个类里，后续任何模块都从 get_settings() 拿，避免散落的硬编码；
  3) 换环境（测试 SQLite / 生产 PostgreSQL）只改 .env，不改代码。
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：backend/
BASE_DIR = Path(__file__).resolve().parent.parent
# 前端静态目录：项目根 /frontend（与 backend 平级）
# 前端静态目录（FastAPI 挂 /app）。两种布局都要支持：
#   本机开发：backend/ 的上级 = 仓库根 → ../frontend
#   容器内  ：WORKDIR=/app，上级是 / → 应取 /app/frontend（挂载点）
_FRONTEND_CANDIDATES = [BASE_DIR.parent / "frontend", BASE_DIR / "frontend"]
FRONTEND_DIR = next((p for p in _FRONTEND_CANDIDATES if p.exists()), _FRONTEND_CANDIDATES[0])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env 里多写的变量不报错，便于渐进添加
    )

    # ---- 应用 ----
    app_name: str = "外贸RAG知识库问答系统"
    app_env: str = "dev"  # dev / test / prod
    log_level: str = "INFO"

    # ---- 数据库（M1 步骤2 起用；M6 起支持 PostgreSQL）----
    # 本地开发：sqlite:///./data/app.db
    # 预发/生产：postgresql+psycopg://rag:密码@127.0.0.1:5432/rag
    database_url: str = "sqlite:///./data/app.db"
    db_auto_create: bool = True        # PG 生产建议 false，改由 alembic upgrade head 建表
    db_pool_size: int = 10             # 连接池常驻连接数（PG）
    db_max_overflow: int = 20          # 峰值可临时增加的连接数
    db_pool_timeout: int = 10          # 借连接最多等几秒，超时报 E5000 而不是无限挂起
    db_pool_recycle: int = 1800        # 连接最长复用 30 分钟（防被中间件静默断开）
    db_pool_pre_ping: bool = True      # 借出前先探活，自动换掉死连接
    db_statement_timeout_ms: int = 10000  # 连接级语句超时兜底（毫秒）
    db_busy_timeout_s: int = 5         # SQLite 写锁等待秒数（仅 SQLite 生效）

    # ---- Redis / Milvus（M2 起用，先占位）----
    redis_url: str = "redis://127.0.0.1:6379/0"
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_collection: str = "chunks_bge-m3_1024"  # PRD 17.4 规则2：命名带模型_维度后缀
    # Milvus RBAC（10-4 起服务端启用 authorizationEnabled）：
    # 默认 root/Milvus 是出厂账号；生产部署用 .env 覆盖为最小权限业务账号
    milvus_user: str = "root"
    milvus_password: str = "Milvus"

    # ---- 向量库 / 嵌入器（M2；本机无 Docker 或无模型时用 memory/hash 兜底）----
    vector_store: str = "memory"  # memory / milvus
    # auto：优先真实模型，失败降级 hash；hash：仅开发测试；bge-m3 / st：强制真实模型
    embed_provider: str = "auto"
    embed_model_name: str = "BAAI/bge-m3"   # sentence-transformers 模型名（或本地目录）
    model_cache_dir: str = "./models"       # 模型下载缓存目录
    hf_endpoint: str = "https://hf-mirror.com"  # 国内镜像（HuggingFace 官方被网络策略阻断）
    embed_batch_size: int = 8
    embed_max_length: int = 1024

    # ---- 重排（M3 FR-205；cross-encoder 需模型，缺失时降级启发式重排）----
    rerank_enabled: bool = True
    rerank_provider: str = "auto"           # auto / cross / heuristic / off
    rerank_model_name: str = "BAAI/bge-reranker-base"
    rerank_top_n: int = 20                  # 参与重排的候选数（重排后再截 top_k）

    # ---- OCR（M2 FR-101/FR-104；扫描件识别 + 低置信度标记）----
    ocr_enabled: bool = True
    ocr_dpi: int = 200                      # 渲染分辨率：200 平衡精度与速度
    ocr_min_text_chars: int = 20            # 文本层少于该字符数 → 判定为扫描页
    ocr_min_confidence: float = 0.85        # 低于该置信度标记人工抽检（FR-104）
    ocr_max_pages: int = 300                # 单文档 OCR 页数上限（防超大文件拖死）

    # ---- 认证（M1 步骤2 起用）----
    secret_key: str = "change-me-to-a-random-secret"
    # 登录图形验证码（防自动化爆破）。评测脚本/集成测试可设为 false 关闭。
    login_captcha_enabled: bool = True
    access_token_expire_minutes: int = 480

    # ---- LLM Provider（M3 起用）----
    # 选择优先级：minimax > deepseek > qwen（谁配了 Key 用谁，只认第一个）
    # 三家都提供 OpenAI 兼容的 POST /chat/completions，所以换厂商只改这三行，不改代码。
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com/v1"  # 国内站；国际站为 api.minimax.io
    minimax_model: str = "MiniMax-M3.1-Flash"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"

    # ---- 本地模型（M2 起用）----
    bge_m3_model_path: str = "./models/bge-m3"
    bge_reranker_model_path: str = "./models/bge-reranker-v2-m3"

    # ---- 文件存储（M2 起用）----
    storage_dir: str = "./data/files"
    max_upload_mb: int = 100  # PRD 7.2：上传 ≤100MB

    # ---- 结构化数据（M4 起用，SQL 五铁律）----
    # 业务库与 RAG 主库物理分离：这里只放汇率/报价/费用/关税视图，
    # 应用用只读连接访问（模拟生产上的 rag_ro 只读账号）。
    trade_db_path: str = "./data/trade.db"
    sql_statement_timeout_ms: int = 3000  # 铁律1：statement_timeout ≤ 3s
    sql_max_rows: int = 200               # 铁律2：最大 200 行
    sql_user_rate_per_min: int = 10       # 铁律2：用户 10 次/分
    sql_session_rate_per_min: int = 20    # 铁律2：会话 20 次/分
    sql_ip_rate_per_min: int = 30         # 铁律2：IP 30 次/分
    sql_global_qps: int = 50              # 铁律2：全局 50 QPS

    # ---- 合规（M4 起用）----
    compliance_llm_check: bool = True      # 第三层 LLM 语义兜底开关（离线评测可关）
    compliance_event_retention_days: int = 180  # PRD FR-308：留存 ≥180 天

    # ---- 异步摄入流水线（M6 步骤10-2）----
    # auto：向量库=memory 时用同进程线程（内存向量库不跨进程，用 Celery 会"索引了但检索不到"）；
    #       向量库=milvus 且有 broker 时用 Celery。
    # celery / thread / inline 可强制指定（inline = 请求内同步执行，仅测试与排障用）。
    ingest_mode: str = "auto"
    ingest_max_attempts: int = 3            # 单任务最多尝试次数（含首次）
    ingest_stale_seconds: int = 900         # worker 崩溃后，超过该秒数允许其他 worker 接管
    ingest_task_soft_time_limit: int = 1800  # 软超时 30 分钟：可被捕获做收尾
    ingest_task_time_limit: int = 2100       # 硬超时 35 分钟：进程级强杀兜底
    celery_queue: str = "ingest"             # 独立队列：上传洪峰不挤占其他任务

    # ---- 可观测 / 评测 / 前端（M5）----
    metrics_enabled: bool = True           # 关闭后埋点变成空操作（压测对比基准时可关）
    metrics_token: str = ""                # 非空时 /metrics 需 Authorization: Bearer <token>
    eval_dir: str = "../eval"              # 评测集目录（随仓库版本化，一行一用例 JSONL）
    eval_report_dir: str = "./data/eval_reports"  # 评测报告归档（运行数据，不入库）
    frontend_enabled: bool = True          # 是否挂载静态前端（/app）

    # ---- Langfuse 全链路追踪（11-1；可选项）----
    # public/secret key 任一为空即自动禁用（SDK 不初始化、埋点全部 no-op），
    # 因此未部署 Langfuse 时无需改任何代码。key 由 langfuse 服务的
    # LANGFUSE_INIT_* 无头初始化预置（见 docker-compose.yml profiles: ["obs"]）。
    langfuse_enabled: bool = True          # 总开关（keys 为空时无论如何都是 no-op）
    langfuse_host: str = "http://langfuse:3000"  # 容器网络内地址；本机直跑用 http://127.0.0.1:13000
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_sample_rate: float = 1.0      # 1.0=全量；压测时可调低


@lru_cache  # 单例缓存：整个进程只读一次 .env
def get_settings() -> Settings:
    settings = Settings()
    # 确保运行数据目录存在（数据库 / 上传文件）
    # 兼容三种 sqlite URL：sqlite:///./data/app.db（相对）、
    # sqlite:////absolute/path/app.db（绝对）、sqlite:///:memory:（内存，跳过）
    if settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_url.removeprefix("sqlite:///")
        if db_path != ":memory:":
            p = Path(db_path)
            if not p.is_absolute():
                p = BASE_DIR / p
            p.parent.mkdir(parents=True, exist_ok=True)
    Path(settings.storage_dir.removeprefix("./")).mkdir(parents=True, exist_ok=True)
    # 本地模型缓存目录（bge-m3 / bge-reranker 下载到此）
    Path(settings.model_cache_dir.removeprefix("./")).mkdir(parents=True, exist_ok=True)
    # 评测集与评测报告目录（M5）：评测集随仓库版本化，报告属运行数据
    eval_path = Path(settings.eval_dir)
    if not eval_path.is_absolute():
        eval_path = BASE_DIR / eval_path
    eval_path.mkdir(parents=True, exist_ok=True)
    Path(settings.eval_report_dir.removeprefix("./")).mkdir(parents=True, exist_ok=True)
    # 业务库（SQL 通道）目录
    trade_path = Path(settings.trade_db_path)
    if not trade_path.is_absolute():
        trade_path = BASE_DIR / trade_path
    trade_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
