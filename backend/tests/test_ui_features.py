"""前端优化配套的后端能力：会话重命名、流式阶段事件、模型配置试连。

这几条都是"界面上的一个按钮"背后的契约，坏了不报错、只是用户点了没反应，
所以单独锁住。
"""
import pytest

from tests.test_chat import _parse_sse


class TestRenameSession:
    def test_rename_ok(self, client, sales_headers):
        # 先造一个会话（发一问）
        resp = client.post("/api/v1/chat", headers=sales_headers, json={"query": "你好"})
        done = [d for e, d in _parse_sse(resp.text) if e == "done"]
        sid = done[-1]["session_id"]

        r = client.patch(f"/api/v1/sessions/{sid}", headers=sales_headers, json={"title": "报关资料清单"})
        assert r.status_code == 200
        assert r.json()["title"] == "报关资料清单"

        listed = client.get("/api/v1/sessions", headers=sales_headers).json()
        assert any(s["id"] == sid and s["title"] == "报关资料清单" for s in listed)

    def test_rename_empty_rejected(self, client, sales_headers):
        resp = client.post("/api/v1/chat", headers=sales_headers, json={"query": "你好"})
        sid = [d for e, d in _parse_sse(resp.text) if e == "done"][-1]["session_id"]
        # 全是空格：schema 的 min_length 挡不住，接口里 strip 后再判空
        r = client.patch(f"/api/v1/sessions/{sid}", headers=sales_headers, json={"title": "   "})
        assert r.status_code == 422

    def test_cannot_rename_others_session(self, client, admin_headers, sales_headers):
        resp = client.post("/api/v1/chat", headers=sales_headers, json={"query": "你好"})
        sid = [d for e, d in _parse_sse(resp.text) if e == "done"][-1]["session_id"]
        r = client.patch(f"/api/v1/sessions/{sid}", headers=admin_headers, json={"title": "越权重命名"})
        assert r.status_code == 404  # 不暴露"存在但无权"，统一 404


class TestStreamStage:
    def test_stage_events_precede_answer(self, client, sales_headers):
        """流式回答前必须发阶段事件，否则前端 2~3 秒只能显示静态'生成中'。"""
        resp = client.post("/api/v1/chat", headers=sales_headers, json={"query": "不锈钢法兰的材质要求"})
        events = _parse_sse(resp.text)
        stages = [d for e, d in events if e == "stage"]
        assert stages, "缺少 stage 事件，前端会以为卡死"
        phases = [s["phase"] for s in stages]
        assert phases[0] == "accepted"  # 第一帧必须是"已接收"
        # 文档问答路径应包含检索阶段（有 stage 才有"正在检索知识库…"）
        assert "retrieval" in phases

    def test_stage_before_done(self, client, sales_headers):
        resp = client.post("/api/v1/chat", headers=sales_headers, json={"query": "你好"})
        names = [e for e, _ in _parse_sse(resp.text)]
        assert "stage" in names
        assert names.index("stage") < names.index("done")


class TestProviderProbe:
    def test_requires_admin(self, client, sales_headers):
        r = client.post("/api/v1/admin/llm-providers/test", headers=sales_headers,
                        json={"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key": "x"})
        assert r.status_code == 403

    def test_bad_scheme_rejected(self, client, admin_headers):
        r = client.post("/api/v1/admin/llm-providers/test", headers=admin_headers,
                        json={"base_url": "api.deepseek.com", "model": "deepseek-chat", "api_key": "sk-x"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and body["error_code"] == "E7101"

    def test_missing_model(self, client, admin_headers):
        r = client.post("/api/v1/admin/llm-providers/test", headers=admin_headers,
                        json={"base_url": "https://api.deepseek.com", "model": "  ", "api_key": "sk-x"})
        assert r.json()["error_code"] == "E7102"

    def test_unreachable_returns_friendly_error(self, client, admin_headers):
        """连不上要立刻返回可读原因，不能把异常抛成 500。"""
        r = client.post("/api/v1/admin/llm-providers/test", headers=admin_headers,
                        json={"base_url": "http://127.0.0.1:1/v1", "model": "x", "api_key": "sk-x"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["error_code"] in {"E7111", "E7112", "E7119"}

    def test_saved_provider_404(self, client, admin_headers):
        r = client.post("/api/v1/admin/llm-providers/999999/test", headers=admin_headers)
        assert r.status_code == 404
