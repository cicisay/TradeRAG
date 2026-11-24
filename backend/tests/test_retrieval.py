"""检索层测试（对应 FR-202/203/603/700/704/707）。

覆盖：
1. 分词与 BM25：中文二元组、精确词（HS 编码/型号）排序靠前
2. RRF 融合：同 chunk 跨路命中合并且分数相加
3. 混合检索：向量路 + BM25 路都能出结果；权限过滤只回本业务组
4. 接口：普通用户（无 debug:retrieval）403；管理员 200；simulate_user 只能管理员用
5. 跨语言：未配 Qwen Key → degraded=True 且 variants 为空（降级而非报错）
"""
import io

import pytest

from app.services.bm25 import BM25Index, tokenize
from app.services.retrieval import Hit, rrf_fuse


class TestBM25:
    def test_tokenize_chinese_and_latin(self):
        tokens = tokenize("不锈钢法兰 DN50 规格")
        assert "不锈" in tokens and "法兰" in tokens
        assert "dn50" in tokens

    def test_ranking_precision(self):
        """精确词（HS 编码）应排在泛化描述前。"""
        index = BM25Index()
        index.build([
            (1, "本文件描述一般贸易流程与注意事项"),
            (2, "商品 HS 编码 7307210000 不锈钢法兰 报关要素"),
            (3, "装箱单与商业发票填写规范"),
        ])
        hits = index.search("HS 编码 7307210000", top_k=3)
        assert hits and hits[0][0] == 2

    def test_empty_index(self):
        assert BM25Index().search("任意查询") == []


class TestRRF:
    def _hit(self, cid, ch):
        return Hit(chunk_id=cid, content=f"c{cid}", score=1.0, channel=ch, channels=[ch])

    def test_fuse_merges_channels(self):
        a = [self._hit(1, "vector"), self._hit(2, "vector")]
        b = [self._hit(2, "bm25"), self._hit(3, "bm25")]
        fused = rrf_fuse([a, b])
        assert fused[0].chunk_id == 2  # 两路都命中 → 分数最高
        assert set(fused[0].channels) == {"vector", "bm25"}
        assert len(fused) == 3

    def test_order_stable(self):
        a = [self._hit(1, "vector"), self._hit(2, "vector")]
        fused = rrf_fuse([a])
        assert [h.chunk_id for h in fused] == [1, 2]


def _upload(client, headers, kb_id, name, text):
    return client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"kb_id": str(kb_id), "doc_type": "spec", "languages": "zh"},
        files={"file": (name, io.BytesIO(text.encode("utf-8")), "text/plain")},
    )


@pytest.fixture(scope="module")
def seeded_docs(client, knowledge_headers, knowledge_headers_b, kb_a_id, kb_b_id):
    """两侧各上传内容，供隔离测试。"""
    r1 = _upload(client, knowledge_headers, kb_a_id, "a组规格.txt",
                 "不锈钢法兰 DN50 材质 304，适用压力 1.6MPa，用于石油管道连接。")
    r2 = _upload(client, knowledge_headers_b, kb_b_id, "b组报价.txt",
                 "客户 B 专属报价：法兰单价 12.5 美元，佣金比例 3%，仅限 B 组查看。")
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    return {"a": r1.json(), "b": r2.json()}


class TestRetrievalAPI:
    def test_permission_denied_for_normal_user(self, client, sales_headers):
        resp = client.post("/api/v1/debug/retrieval", headers=sales_headers, json={"query": "法兰"})
        assert resp.status_code == 403

    def test_admin_hybrid_results(self, client, admin_headers, seeded_docs):
        resp = client.post("/api/v1/debug/retrieval", headers=admin_headers,
                           json={"query": "不锈钢法兰 DN50", "top_k": 5})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["vector"], "向量路应有结果"
        assert data["bm25"], "BM25 路应有结果"
        assert data["hybrid"], "混合路应有结果"
        # 排序：相关块应在前
        top = data["hybrid"][0]
        assert "法兰" in top["content"]
        assert top["channels"]

    def test_biz_group_isolation_via_simulation(self, client, admin_headers, seeded_docs):
        """FR-704：以 biz_a 知识管理员身份检索，绝不能召回 biz_b 的报价内容。"""
        resp = client.post("/api/v1/debug/retrieval", headers=admin_headers,
                           json={"query": "客户报价 佣金", "top_k": 10, "simulate_user": "knowledge_a"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["effective_user"]["biz_group"] == "biz_a"
        for channel in ("vector", "bm25", "hybrid"):
            for hit in data[channel]:
                assert hit["biz_group"] == "biz_a", f"{channel} 通道越权召回了 {hit['biz_group']}"

    def test_simulate_user_admin_only(self, client, sales_headers):
        resp = client.post("/api/v1/debug/retrieval", headers=sales_headers,
                           json={"query": "x", "simulate_user": "admin"})
        assert resp.status_code == 403

    def test_crosslingual_without_translator_not_degraded(self, client, admin_headers, monkeypatch):
        """未配置翻译模型：不报错、不生成变体，且**不算降级**。

        口径修正（M5）：degraded 表示"尝试了但失败"。从未配置翻译模型属于"未启用"，
        若也算降级，开发环境的降级率会恒为 100%，监控指标完全失真。
        """
        # 模拟"未配置任何翻译模型"：清空 env Key + 失效 llm_config 链缓存。
        # 不清的话 .env 里的真实 Key 会让翻译路真的发外网请求（慢且不确定）。
        from app.config import get_settings
        from app.services.llm_config import invalidate as invalidate_llm_chain

        settings = get_settings()
        with monkeypatch.context() as mp:
            for field in ("minimax_api_key", "deepseek_api_key", "qwen_api_key"):
                mp.setattr(settings, field, "")
            invalidate_llm_chain()
            resp = client.post("/api/v1/debug/retrieval", headers=admin_headers,
                               json={"query": "法兰规格", "crosslingual": True})
            assert resp.status_code == 200
            data = resp.json()
        assert data["degraded"] is False
        assert data["variants"] == {}
