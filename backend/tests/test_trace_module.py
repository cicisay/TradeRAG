"""链路追踪接口（11-3）的单元测试。

不打真实 Langfuse：验证投影、截断、空状态三条关键行为。
"""
from app.api import traces


def test_question_of_handles_three_shapes():
    assert traces._question_of({"input": "  你好  世界 "}) == "你好 世界"
    assert traces._question_of({"input": {"query": "查一下法兰"}}) == "查一下法兰"
    assert "法兰" in traces._question_of({"input": [{"role": "user", "content": "法兰的材质"}]})
    assert traces._question_of({"input": None}) == ""


def test_question_of_truncates_long_text():
    q = traces._question_of({"input": "问" * 500})
    assert len(q) == traces._QUESTION_MAX + 1 and q.endswith("…")


def test_clip_truncates_only_when_needed():
    short = {"a": 1}
    assert traces._clip(short) == short
    long_text = "x" * 5000
    out = traces._clip(long_text)
    assert isinstance(out, str) and "已截断" in out and len(out) < 5000
    assert traces._clip(None) is None


def test_list_without_key_returns_empty_state(monkeypatch):
    monkeypatch.setattr(traces, "_auth", lambda: None)
    res = traces.list_traces(hours=24, limit=10, user=None)
    assert res["enabled"] is False
    assert res["reachable"] is False
    assert res["items"] == []
    assert "未配置" in res["reason"]


def test_list_projects_fields(monkeypatch):
    monkeypatch.setattr(traces, "_auth", lambda: ("pk", "sk"))
    payload = {
        "data": [{
            "id": "abc123", "name": "chat", "timestamp": "2026-11-16T13:37:37.372Z",
            "latency": 22.1, "userId": "3", "sessionId": "84",
            "input": "不锈钢法兰的材质要求？",
            "metadata": {"branch": "kb", "refs": 5, "intent": "kb_question", "took_ms": 22138},
            "observations": ["a", "b", "c"], "scores": [], "level": "DEFAULT",
        }],
        "meta": {"totalItems": 12},
    }
    monkeypatch.setattr(traces, "_call", lambda path, params=None: payload)
    res = traces.list_traces(hours=24, limit=10, user=None)
    assert res["reachable"] is True and res["total"] == 12
    item = res["items"][0]
    assert item["branch"] == "kb" and item["refs"] == 5 and item["nodes"] == 3
    assert item["question"] == "不锈钢法兰的材质要求？"


def test_list_unreachable_keeps_shape(monkeypatch):
    """Langfuse 没起时不能抛 500，要让前端拿到可读原因。"""
    monkeypatch.setattr(traces, "_auth", lambda: ("pk", "sk"))

    def boom(path, params=None):
        raise traces._Unreachable("连不上 Langfuse，追踪服务未启动")

    monkeypatch.setattr(traces, "_call", boom)
    res = traces.list_traces(hours=24, limit=10, user=None)
    assert res["enabled"] is True and res["reachable"] is False
    assert "连不上" in res["reason"] and res["items"] == []


def test_detail_keeps_parent_link_and_clips(monkeypatch):
    monkeypatch.setattr(traces, "_auth", lambda: ("pk", "sk"))
    trace = {
        "id": "abc123", "name": "chat", "timestamp": "2026-11-16T13:37:37.372Z",
        "latency": 22.1, "input": "问题", "output": "答案",
        "metadata": {"branch": "kb", "refs": 5},
        "observations": [
            {"id": "o1", "name": "retrieval", "type": "SPAN", "latency": 13.8,
             "parentObservationId": None, "startTime": "2026-11-16T13:37:37.378Z",
             "endTime": "2026-11-16T13:37:51.248Z", "input": {"q": 1}, "output": {"n": 5}},
            {"id": "o2", "name": "vector-channel", "type": "SPAN", "latency": 3.8,
             "parentObservationId": "o1", "output": "x" * 6000},
        ],
    }
    monkeypatch.setattr(traces, "_call", lambda path: trace)
    res = traces.get_trace("abc123", user=None)
    assert res["trace"]["branch"] == "kb"
    assert len(res["observations"]) == 2
    assert res["observations"][1]["parentId"] == "o1"
    assert "已截断" in res["observations"][1]["output"]
    assert res["observations"][0]["output"] == {"n": 5}
