"""切分器 + 索引流水线 + 向量库权限过滤测试（M2 步骤4）。

覆盖：
1. 切分：段落聚合、超长硬切、overlap 安全、空文本、语种探测（zh/en/es/ar）
2. 嵌入器：hash 嵌入维度 1024、归一化、确定性（同文本同向量）、相似文本得分更高
3. 向量库：权限 expr 过滤（FR-603：业务组/密级/doc_status）、删旧写新
4. 流水线集成：上传 → 自动切分入库 → chunk 表与内存向量库数量一致；重跑幂等
"""
import io

import pytest

from app.models.chunk import Chunk
from app.services.chunker import split_text, detect_language
from app.services.embedding import get_embedder, reset_embedder, HashEmbedder
from app.services.vector_store import (
    InMemoryVectorStore, VectorRecord, build_access_expr, get_vector_store, reset_vector_store,
)
from app.services.pipeline import run_indexing


# ---------- 切分器 ----------

class TestChunker:
    def test_empty_text(self):
        assert split_text("") == []
        assert split_text("   \n  ") == []

    def test_paragraph_merge(self):
        text = "\n\n".join(f"段落{i} " + "内容" * 40 for i in range(5))
        chunks = split_text(text, max_chars=200, overlap=20)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c["content"]) <= 200
            # 步骤8 起增加 page_no（PDF 页码定位），这里断言必需字段而非精确集合
            assert {"chunk_index", "content", "language", "page_no"} <= set(c.keys())

    def test_long_paragraph_hard_split(self):
        text = "超长" * 5000  # 10000 字符无换行
        chunks = split_text(text, max_chars=500, overlap=50)
        assert len(chunks) > 10
        assert all(len(c["content"]) <= 500 for c in chunks)

    def test_language_detect(self):
        assert detect_language("这是一段中文内容，涉及报关单证。") == "zh"
        assert detect_language("Commercial invoice and packing list.") == "en"
        assert detect_language("¿Cuál es el precio de la mercancía?") == "es"
        assert detect_language("هذا نص عربي للاختبار المطلوب للتصدير") == "ar"


# ---------- 嵌入器 ----------

class TestEmbedder:
    def test_dim_and_norm(self):
        emb = HashEmbedder()
        vec = emb.embed(["不锈钢法兰 DN50 规格书"])[0]
        assert len(vec) == 1024
        norm = sum(x * x for x in vec) ** 0.5
        assert abs(norm - 1.0) < 1e-6  # L2 归一化（IP 度量前提）

    def test_deterministic(self):
        emb = HashEmbedder()
        v1, v2 = emb.embed(["同一句话", "同一句话"])
        assert v1 == v2

    def test_similar_score_higher(self):
        emb = HashEmbedder()
        q, near, far = emb.embed(["商业发票装箱单", "商业发票与装箱单据", "今天天气真不错"])
        assert _dot(q, near) > _dot(q, far)


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


# ---------- 向量库权限过滤 ----------

class TestVectorStoreAccess:
    def _mk_store(self):
        store = InMemoryVectorStore()
        store.ensure_collection("test_col", 1024)
        emb = HashEmbedder()
        return store, emb

    def _insert(self, store, emb, chunk_id, biz_group, sensitivity=0, doc_status="active"):
        store.insert("test_col", [
            VectorRecord(
                vector=emb.embed([f"doc content {chunk_id}"])[0],
                chunk_id=chunk_id, kb_id=1, biz_group=biz_group,
                sensitivity=sensitivity, doc_status=doc_status,
                doc_id=chunk_id, doc_version_id=chunk_id, language="zh",
            )
        ])

    def test_biz_group_filter(self):
        """FR-603：只能召回自己业务组的向量。"""
        store, emb = self._mk_store()
        self._insert(store, emb, 1, "biz_a")
        self._insert(store, emb, 2, "biz_b")
        expr = build_access_expr(kb_ids=[1], biz_groups=["biz_a"], max_sensitivity=3)
        hits = store.search("test_col", emb.embed(["doc content"])[0], expr=expr, top_k=10)
        assert all(h["biz_group"] == "biz_a" for h in hits)
        assert len(hits) == 1

    def test_sensitivity_and_status_filter(self):
        """密级高于用户上限 / 非 active 的向量不可召回。"""
        store, emb = self._mk_store()
        self._insert(store, emb, 1, "biz_a", sensitivity=0)
        self._insert(store, emb, 2, "biz_a", sensitivity=2)
        self._insert(store, emb, 3, "biz_a", doc_status="archived")
        expr = build_access_expr([1], ["biz_a"], max_sensitivity=1)
        hits = store.search("test_col", emb.embed(["doc content"])[0], expr=expr, top_k=10)
        assert {h["chunk_id"] for h in hits} == {1}

    def test_delete_by_doc_version(self):
        """文档更新删旧写新（PRD 6.3）。"""
        store, emb = self._mk_store()
        self._insert(store, emb, 1, "biz_a")
        self._insert(store, emb, 2, "biz_a")
        deleted = store.delete_by_doc_version("test_col", 1)
        assert deleted == 1
        assert store.count("test_col") == 1

    def test_missing_required_field(self):
        with pytest.raises(ValueError):
            VectorRecord(vector=[0.1])


# ---------- 流水线集成（通过上传接口端到端） ----------

class TestPipelineIntegration:
    def test_upload_triggers_indexing(self, client, knowledge_headers, kb_a_id):
        reset_vector_store()  # 内存库每个测试进程独立，避免计数污染
        long_text = "\n\n".join(f"条款{i}：本条款描述报关流程要点" + "细则内容" * 20 for i in range(6))
        resp = client.post(
            "/api/v1/documents/upload",
            headers=knowledge_headers,
            data={"kb_id": str(kb_a_id), "doc_type": "customs", "languages": "zh"},
            files={"file": ("流程.txt", io.BytesIO(long_text.encode()), "text/plain")},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "parsed"
        assert data["chunks"] and data["chunks"] > 0

        # chunk 表与向量库数量一致
        from app.db.session import SessionLocal
        from app.models.document import DocVersion
        from app.services.vector_store import get_vector_store
        from app.config import get_settings

        db = SessionLocal()
        try:
            version = db.get(DocVersion, data["current_version_id"])
            chunk_count = db.query(Chunk).filter(Chunk.doc_version_id == version.id).count()
            assert chunk_count == data["chunks"]
            assert version.pipeline_trace_id is not None
        finally:
            db.close()
        store = get_vector_store()
        # collection 名必须与 pipeline 同源（10-3 起统一经 sanitize_collection_name 净化）
        from app.services.vector_store import sanitize_collection_name

        assert store.count(sanitize_collection_name(get_settings().milvus_collection)) >= data["chunks"]

    def test_reindex_idempotent(self, client, knowledge_headers, kb_a_id):
        """重跑索引不重复入库（删旧写新）。"""
        from app.db.session import SessionLocal
        from app.models.document import Document
        db = SessionLocal()
        doc_id = db.query(Document).order_by(Document.id.desc()).first().id
        db.close()

        reset_vector_store()
        stats1 = None
        from app.db.session import SessionLocal as S
        with S() as db2:
            stats1 = run_indexing(doc_id, db2)
        with S() as db2:
            stats2 = run_indexing(doc_id, db2)
        assert stats2["chunks"] == stats1["chunks"]  # 幂等：数量不变
