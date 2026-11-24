"""问答链路测试（对应 FR-206 引用红线 / FR-209 流式 / FR-213 边界 / FR-401 会话）。

覆盖：
1. 知识库提问：SSE 事件序列 meta → delta → done；meta 带引用且为原文快照
2. 引用快照确实落库（ref_snapshot.content_snapshot 与命中 chunk 原文一致）
3. 会话列表与历史可查；他人会话不可见
4. 问候走模板回答（无引用）
5. 未配 LLM Key：诚实兜底（不编造，列出原文依据）
6. 未登录 401
"""
import io
import json

import pytest


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成 [(event, data_dict)]。"""
    events = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: "):])
        if event_name:
            events.append((event_name, json.loads("\n".join(data_lines) or "{}")))
    return events


@pytest.fixture(scope="module")
def chat_doc(client, knowledge_headers, kb_a_id):
    """上传一份可命中的文档。"""
    text = ("不锈钢法兰产品规格书\n\n"
            "材质：304 不锈钢；规格：DN50；公称压力：1.6MPa；适用介质：水、油、气体。\n\n"
            "包装：木箱，每箱 20 件；原产地：中国。")
    resp = client.post(
        "/api/v1/documents/upload",
        headers=knowledge_headers,
        data={"kb_id": str(kb_a_id), "doc_type": "spec", "languages": "zh"},
        files={"file": ("法兰规格书.txt", io.BytesIO(text.encode("utf-8")), "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestChatSSE:
    def test_kb_question_stream(self, client, sales_headers, chat_doc):
        resp = client.post("/api/v1/chat", headers=sales_headers,
                           json={"query": "不锈钢法兰的材质和压力等级是什么？"})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        events = _parse_sse(resp.text)
        names = [e[0] for e in events]
        # 首帧是 stage（前端据此显示"已接收/正在检索"），meta 紧随其后
        assert names[0] == "stage"
        assert names.index("stage") < names.index("meta")
        assert names[-1] == "done"
        assert "delta" in names

        meta = next(d for e, d in events if e == "meta")
        assert meta["intent"] == "kb_question"
        assert meta["refs"], "回答必须附带引用（FR-206 红线）"
        assert any("法兰" in r["content_snapshot"] for r in meta["refs"])

        answer = "".join(d["content"] for e, d in events if e == "delta")
        assert answer.strip(), "必须有回答内容"

    def test_ref_snapshot_persisted(self, client, sales_headers, chat_doc):
        resp = client.post("/api/v1/chat", headers=sales_headers,
                           json={"query": "法兰的公称压力是多少？"})
        done = _parse_sse(resp.text)[-1][1]
        message_id = done["message_id"]
        assert done["refs"] >= 1

        from app.db.session import SessionLocal
        from app.models.chat import RefSnapshot

        db = SessionLocal()
        try:
            refs = db.query(RefSnapshot).filter(RefSnapshot.message_id == message_id).all()
            assert refs
            assert refs[0].content_snapshot.strip()
            assert "法兰" in refs[0].content_snapshot  # 原文快照不是空壳
            assert refs[0].doc_name.endswith(".txt")
        finally:
            db.close()

    def test_greeting_no_refs(self, client, sales_headers):
        resp = client.post("/api/v1/chat", headers=sales_headers, json={"query": "你好"})
        events = _parse_sse(resp.text)
        meta = next(d for e, d in events if e == "meta")
        assert meta["intent"] == "greeting"
        assert meta["refs"] == []
        assert events[-1][1]["refs"] == 0

    def test_unauthorized(self, client):
        resp = client.post("/api/v1/chat", json={"query": "法兰"})
        assert resp.status_code == 401


class TestSessions:
    def test_list_and_history(self, client, sales_headers, chat_doc):
        resp = client.post("/api/v1/chat", headers=sales_headers,
                           json={"query": "法兰适合什么介质？"})
        session_id = _parse_sse(resp.text)[0][1]["session_id"]

        listed = client.get("/api/v1/sessions", headers=sales_headers).json()
        assert any(s["id"] == session_id for s in listed)

        history = client.get(f"/api/v1/sessions/{session_id}/messages", headers=sales_headers)
        assert history.status_code == 200
        msgs = history.json()
        assert len(msgs) == 2  # 一问一答
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["refs"], "历史里也要能取到引用快照"

    def test_other_user_cannot_read_session(self, client, sales_headers, knowledge_headers_b, chat_doc):
        resp = client.post("/api/v1/chat", headers=sales_headers, json={"query": "法兰的规格？"})
        session_id = _parse_sse(resp.text)[0][1]["session_id"]
        resp2 = client.get(f"/api/v1/sessions/{session_id}/messages", headers=knowledge_headers_b)
        assert resp2.status_code == 404


class TestFallback:
    def test_no_llm_key_honest_fallback(self, client, sales_headers, chat_doc, monkeypatch):
        """未配置 LLM Key：回答中明确说明，不编造结论（FR-213）。

        注意：不能依赖"运行环境恰好没有 Key"——开发机 .env 里可能配了真实 Key。
        这里在 monkeypatch 上下文里把三家 key 都清空，退出后恢复并重建客户端，
        保证测试结果不受本地配置影响。
        """
        from app.config import get_settings
        from app.services.llm import reset_llm_client
        from app.services.llm_config import invalidate as invalidate_llm_chain

        settings = get_settings()
        with monkeypatch.context() as mp:
            for field in ("minimax_api_key", "deepseek_api_key", "qwen_api_key"):
                mp.setattr(settings, field, "")
            reset_llm_client()
            invalidate_llm_chain()  # 清空 env Key 后必须失效 30s 链缓存，否则上一轮配置还在
            resp = client.post("/api/v1/chat", headers=sales_headers, json={"query": "法兰的包装方式？"})
            answer = "".join(d["content"] for e, d in _parse_sse(resp.text) if e == "delta")
        reset_llm_client()  # 恢复真实配置对应的客户端
        invalidate_llm_chain()

        assert "未配置大模型" in answer
        assert "法兰" in answer  # 至少给出了原文依据


class TestDoneEventContract:
    """done 事件契约：每条回答都必须报告 llm_provider。

    回归背景：早先四条返回分支各写一份 done dict，其中合规拦截 / 问候 / SQL 数据查询
    三条漏了 llm_provider，前端把「汇率查询成功」显示成了「未接大模型」。
    这里锁住字段存在性，后端新增分支时若忘了用 _done() 会立刻失败。
    """

    @pytest.mark.parametrize("query", ["你好", "法兰的包装方式？"])
    def test_done_always_reports_provider(self, client, sales_headers, monkeypatch, query):
        from app.config import get_settings
        from app.services.llm import reset_llm_client
        from app.services.llm_config import invalidate as invalidate_llm_chain

        settings = get_settings()
        with monkeypatch.context() as mp:
            for field in ("minimax_api_key", "deepseek_api_key", "qwen_api_key"):
                mp.setattr(settings, field, "")
            reset_llm_client()
            invalidate_llm_chain()  # 清空 env Key 后必须失效 30s 链缓存，否则上一轮配置还在
            resp = client.post("/api/v1/chat", headers=sales_headers, json={"query": query})
        reset_llm_client()

        done = [d for e, d in _parse_sse(resp.text) if e == "done"]
        assert done, "缺少 done 事件"
        assert "llm_provider" in done[-1], "done 缺 llm_provider，前端会误显示「未接大模型」"
        assert done[-1]["llm_provider"] in {"none", "minimax", "deepseek", "qwen"}
