"""pytest 公共夹具：测试专用数据库隔离。

给新手的说明：
- 测试绝不能碰真实数据（data/app.db），这里把环境变量指向 data/test_app.db，
  并在任何 app 模块导入之前设置（否则连接池已经用默认库建好了）。
- 测试结束后删除测试库文件，保证每次跑测试都是干净状态。
"""
import os
from pathlib import Path

_TEST_DB = Path(__file__).resolve().parent.parent / "data" / "test_app.db"
_TEST_TRADE_DB = Path(__file__).resolve().parent.parent / "data" / "test_trade.db"

# 必须在导入 app.* 之前设置环境变量
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"
os.environ["TRADE_DB_PATH"] = str(_TEST_TRADE_DB)
os.environ["APP_ENV"] = "test"
# 测试默认不加载真实模型：哈希嵌入 + 启发式重排，保证快速、确定、无网络依赖。
# 需要验证真实模型的用例单独用 monkeypatch 覆盖（见 test_models.py）。
os.environ.setdefault("EMBED_PROVIDER", "hash")
os.environ.setdefault("RERANK_PROVIDER", "heuristic")
# 异步摄入在测试里走同步执行：一是结果确定（上传返回即可断言 parsed），
# 二是不依赖 Redis/Celery，CI 不需要额外服务。
os.environ.setdefault("INGEST_MODE", "inline")
# 集成测试走无验证码模式（148 条既有用例都直接 POST login）；验证码本身单独测（test_captcha.py）
os.environ.setdefault("LOGIN_CAPTCHA_ENABLED", "false")
# 10-3 安全钉：config.py 会读 .env，本机 .env 已切 VECTOR_STORE=milvus——
# 测试必须强制回内存向量库，否则全量测试会把向量写进真实 Milvus collection；
# Redis 同理钉回测试实例，避免测试任务污染队列数据。
os.environ["VECTOR_STORE"] = "memory"
os.environ["REDIS_URL"] = "redis://127.0.0.1:56379/0"
# 11-1 安全钉：.env 里已配 Langfuse key，测试进程不追踪（不上报、不连网、无日志噪音）
os.environ["LANGFUSE_ENABLED"] = "false"

if _TEST_DB.exists():
    _TEST_DB.unlink()  # 每次全新开始
if _TEST_TRADE_DB.exists():
    _TEST_TRADE_DB.unlink()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.user import User, Role  # noqa: E402
from app.core.security import hash_password  # noqa: E402


@pytest.fixture(scope="session")
def client():
    """启动测试客户端（lifespan 会自动建表 + 种子角色与管理员）。"""
    with TestClient(app) as c:
        # 追加测试账号与两个业务组的知识库（用于上传与隔离测试）
        db = SessionLocal()
        try:
            from app.models.knowledge_base import KnowledgeBase

            role_user = db.query(Role).filter(Role.code == "user").first()
            role_knowledge = db.query(Role).filter(Role.code == "knowledge").first()

            db.add(User(
                username="sales01",
                password_hash=hash_password("sales123456"),
                role_id=role_user.id,
                biz_group="biz_a",
            ))
            kn_a = User(
                username="knowledge_a",
                password_hash=hash_password("kn12345678"),
                role_id=role_knowledge.id,
                biz_group="biz_a",
            )
            kn_b = User(
                username="knowledge_b",
                password_hash=hash_password("kn12345678"),
                role_id=role_knowledge.id,
                biz_group="biz_b",
            )
            db.add_all([kn_a, kn_b])
            db.flush()
            db.add_all([
                KnowledgeBase(name="kb-a-产品资料", biz_group="biz_a", created_by="knowledge_a"),
                KnowledgeBase(name="kb-b-单证资料", biz_group="biz_b", created_by="knowledge_b"),
            ])
            db.commit()
        finally:
            db.close()
        yield c


def _login(client: TestClient, username: str, password: str) -> dict:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture(scope="session")
def admin_headers(client):
    token = _login(client, "admin", "admin123")["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def sales_headers(client):
    token = _login(client, "sales01", "sales123456")["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def knowledge_headers(client):
    token = _login(client, "knowledge_a", "kn12345678")["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def knowledge_headers_b(client):
    token = _login(client, "knowledge_b", "kn12345678")["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def kb_a_id(client):
    """biz_a 知识库 id。"""
    from app.db.session import SessionLocal
    from app.models.knowledge_base import KnowledgeBase

    db = SessionLocal()
    try:
        return db.query(KnowledgeBase).filter(KnowledgeBase.biz_group == "biz_a").first().id
    finally:
        db.close()


@pytest.fixture(scope="session")
def kb_b_id(client):
    """biz_b 知识库 id。"""
    from app.db.session import SessionLocal
    from app.models.knowledge_base import KnowledgeBase

    db = SessionLocal()
    try:
        return db.query(KnowledgeBase).filter(KnowledgeBase.biz_group == "biz_b").first().id
    finally:
        db.close()
