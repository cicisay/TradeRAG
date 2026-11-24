"""向量库适配层单测（10-3 新增：Milvus 真机联调发现的修复回归）。

全部只测纯函数/内存实现，不依赖真实 Milvus（Milvus 侧验证见 step-10-3 文档的联调记录）。
"""
from app.services.vector_store import InMemoryVectorStore, build_access_expr, sanitize_collection_name


class TestCollectionName:
    def test_sanitize_hyphen(self):
        """bge-m3 的连字符在 Milvus 非法，必须归一为下划线（10-3 真机报错 E1100）。"""
        assert sanitize_collection_name("chunks_bge-m3_1024") == "chunks_bge_m3_1024"

    def test_sanitize_keeps_legal(self):
        assert sanitize_collection_name("chunks_bge_m3_1024") == "chunks_bge_m3_1024"

    def test_sanitize_all_bad_chars(self):
        assert sanitize_collection_name("a.b:c-d e") == "a_b_c_d_e"


class TestAccessExpr:
    def test_expr_covers_all_four_filters(self):
        expr = build_access_expr(kb_ids=[1, 2], biz_groups=["default"], max_sensitivity=0)
        assert "kb_id in [1,2]" in expr
        assert 'biz_group in ["default"]' in expr
        assert "sensitivity <= 0" in expr
        assert 'doc_status == "active"' in expr

    def test_expr_escapes_biz_group_quotes(self):
        """组名含引号时不允许逃逸出字符串字面量（防 expr 注入）。"""
        expr = build_access_expr(kb_ids=[1], biz_groups=['a" or 1==1 or "'], max_sensitivity=0)
        # Milvus expr 字符串转义：内部双引号必须带反斜杠，否则 or 1==1 会被当语法解析
        assert 'biz_group in ["a\\" or 1==1 or \\""]' in expr


class TestInMemoryParity:
    def test_delete_by_doc_version(self):
        store = InMemoryVectorStore()
        store.ensure_collection("c", 4)
        from app.services.vector_store import VectorRecord

        store.insert("c", [VectorRecord(vector=[0.1] * 4, chunk_id=1, doc_id=9, doc_version_id=77,
                                        kb_id=1, biz_group="default", content_hash="9-1-0")])
        assert store.count("c") == 1
        store.delete_by_doc_version("c", 77)
        assert store.count("c") == 0
