"""多模型配置（管理后台）测试：权限、CRUD、掩码、主用切换、加密落库、调用链兜底。"""
import pytest


@pytest.fixture(autouse=True)
def _clean_llm_config():
    """每个用例前后清空 llm_provider_config：测试共享同一个库，配置互相污染会导致链序断言失败。"""
    from app.db.session import SessionLocal
    from app.models.llm_config import LlmProviderConfig
    from app.services.llm_config import invalidate

    def _wipe() -> None:
        db = SessionLocal()
        try:
            db.query(LlmProviderConfig).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        invalidate()

    _wipe()
    yield
    _wipe()


@pytest.fixture()
def provider_body():
    return {
        "name": "MiniMax 主力",
        "provider": "minimax",
        "base_url": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M3.1-Flash",
        "api_key": "sk-test-secret-key-0001",
        "remark": "测试配置",
    }


def _create(client, admin_headers, body):
    resp = client.post("/api/v1/admin/llm-providers", headers=admin_headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_non_admin_forbidden(client, sales_headers):
    resp = client.get("/api/v1/admin/llm-providers", headers=sales_headers)
    assert resp.status_code == 403


def test_unauthorized_rejected(client):
    resp = client.get("/api/v1/admin/llm-providers")
    assert resp.status_code == 401


def test_create_returns_masked_key_and_encrypts_at_rest(client, admin_headers, provider_body):
    created = _create(client, admin_headers, provider_body)
    assert created["is_primary"] is True  # 第一条自动成为主用

    lst = client.get("/api/v1/admin/llm-providers", headers=admin_headers).json()
    row = next(r for r in lst if r["id"] == created["id"])
    assert row["key_hint"].endswith("0001")
    assert "sk-test-secret" not in row["key_hint"]

    # 落库必须是密文：明文 Key 不能出现在 api_key_enc 字段里
    from app.db.session import SessionLocal
    from app.models.llm_config import LlmProviderConfig

    db = SessionLocal()
    try:
        stored = db.get(LlmProviderConfig, created["id"]).api_key_enc
        assert "sk-test-secret-key-0001" not in stored
    finally:
        db.close()


def test_update_without_key_keeps_old_key(client, admin_headers, provider_body):
    created = _create(client, admin_headers, provider_body)
    resp = client.put(
        f"/api/v1/admin/llm-providers/{created['id']}",
        headers=admin_headers,
        json={**provider_body, "api_key": "", "model": "MiniMax-M2.7"},  # 不传新 Key
    )
    assert resp.status_code == 200
    lst = client.get("/api/v1/admin/llm-providers", headers=admin_headers).json()
    row = next(r for r in lst if r["id"] == created["id"])
    assert row["model"] == "MiniMax-M2.7"
    assert row["key_hint"].endswith("0001")  # Key 未被掩码污染


def test_activate_and_chain_order(client, admin_headers):
    a = _create(client, admin_headers, {
        "name": "厂商A", "provider": "deepseek", "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat", "api_key": "sk-chain-a-aaaa",
    })
    b = _create(client, admin_headers, {
        "name": "厂商B", "provider": "qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus", "api_key": "sk-chain-b-bbbb",
    })

    # a 已是第一条自动主用；把 b 设为主用后链应 b → a
    resp = client.post(f"/api/v1/admin/llm-providers/{b['id']}/activate", headers=admin_headers)
    assert resp.status_code == 200

    from app.db.session import SessionLocal
    from app.services.llm_config import get_provider_chain, invalidate

    invalidate()
    db = SessionLocal()
    try:
        chain = get_provider_chain(db)
        assert chain[0].ref_id == b["id"]
        assert chain[0].provider == "qwen"
        assert [e.ref_id for e in chain] == [b["id"], a["id"]]
    finally:
        db.close()


def test_disable_primary_falls_back(client, admin_headers):
    a = _create(client, admin_headers, {
        "name": "主用A", "provider": "deepseek", "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat", "api_key": "sk-fb-a-aaaa",
    })
    _create(client, admin_headers, {
        "name": "备用B", "provider": "qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus", "api_key": "sk-fb-b-bbbb",
    })
    # 禁用主用 A：主用自动落到 B，链上不应再出现禁用项
    resp = client.post(f"/api/v1/admin/llm-providers/{a['id']}/toggle", headers=admin_headers)
    assert resp.status_code == 200 and resp.json()["enabled"] is False

    from app.db.session import SessionLocal
    from app.services.llm_config import get_provider_chain, invalidate

    invalidate()
    db = SessionLocal()
    try:
        chain = get_provider_chain(db)
        assert chain and all(e.ref_id != a["id"] for e in chain)
    finally:
        db.close()


def test_delete_provider(client, admin_headers, provider_body):
    created = _create(client, admin_headers, provider_body)
    resp = client.delete(f"/api/v1/admin/llm-providers/{created['id']}", headers=admin_headers)
    assert resp.status_code == 200
    lst = client.get("/api/v1/admin/llm-providers", headers=admin_headers).json()
    assert all(r["id"] != created["id"] for r in lst)


def test_env_fallback_when_no_db_config(client, admin_headers):
    """清空所有 DB 配置后，链应回退到 .env（本地有 MiniMax Key）而不是空链。"""
    lst = client.get("/api/v1/admin/llm-providers", headers=admin_headers).json()
    for r in lst:
        client.delete(f"/api/v1/admin/llm-providers/{r['id']}", headers=admin_headers)

    from app.db.session import SessionLocal
    from app.services.llm_config import get_provider_chain, invalidate

    invalidate()
    db = SessionLocal()
    try:
        chain = get_provider_chain(db)
        assert chain, "无 DB 配置时应回退 env 链"
        assert chain[0].source == "env"
    finally:
        db.close()


def test_presets_endpoint(client, admin_headers):
    resp = client.get("/api/v1/admin/llm-providers/presets", headers=admin_headers)
    assert resp.status_code == 200
    presets = resp.json()["presets"]
    assert "minimax" in presets and "openai" in presets
    assert presets["ollama"]["base_url"].startswith("http://127.0.0.1")
