"""步骤 8 测试：OCR 扫描件识别 + 页码定位 + 真实模型/重排。

给新手的说明：
- 这里验证三件事：
  ① 扫描件（纯图片 PDF）能通过 OCR 拿到文字，并记录置信度（FR-104）；
  ② 切分后的 chunk 能知道自己来自第几页（引用定位，FR-206）；
  ③ 重排确实会把"更相关的候选"提到前面。
- 真实语义模型（bge-m3）需要联网下载数 GB 模型，测试默认跳过；
  设置环境变量 RUN_MODEL_TESTS=1 才运行（本地已下载模型时用）。
"""
import os

import pytest

from app.services.chunker import split_text
from app.services.ocr import reset_ocr_engine
from app.services.parser import PAGE_SEPARATOR, page_spans, parse_file
from app.services.reranker import HeuristicReranker, query_units


def _scanned_pdf(text: str) -> bytes:
    """造一个"纯图片 PDF"：把文字先渲染成图片，再作为图片插入新 PDF。

    这样 PDF 里没有任何文本层——正是扫描件的特征，只有 OCR 才能读出内容。
    """
    import pymupdf

    src = pymupdf.open()
    page = src.new_page(width=595, height=842)  # A4
    page.insert_text((72, 120), text, fontsize=18)
    pixmap = page.get_pixmap(dpi=150)
    png = pixmap.tobytes("png")
    src.close()

    out = pymupdf.open()
    target = out.new_page(width=595, height=842)
    target.insert_image(target.rect, stream=png)
    data = out.tobytes()
    out.close()
    return data


class TestOcr:
    def test_scanned_pdf_requires_ocr(self):
        """扫描件（无文本层）应由 OCR 读出文字并给出置信度。"""
        reset_ocr_engine()
        data = _scanned_pdf("INVOICE 2026 HS CODE 85395000")
        result = parse_file("scan.pdf", data)

        assert result.ocr_used is True, f"未触发 OCR，error={result.error} ocr_error={result.ocr_error}"
        assert result.text.strip(), "OCR 未产出文本"
        assert result.ocr_confidence is not None
        assert 0 < result.ocr_confidence <= 1
        # 至少识别出字母或数字（不苛求逐字正确，OCR 有误差）
        assert any(ch.isalnum() for ch in result.text)

    def test_ocr_page_marking(self, client, knowledge_headers, kb_a_id):
        """上传扫描件后：文档带 OCR 质量字段，chunk 带页码。"""
        data = _scanned_pdf("PACKING LIST GROSS WEIGHT 1200 KGS")
        resp = client.post(
            "/api/v1/documents/upload",
            headers=knowledge_headers,
            data={"kb_id": str(kb_a_id), "doc_type": "customs", "languages": "en"},
            files={"file": ("scan_packing.pdf", data, "application/pdf")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "parsed", body
        assert body["ocr_used"] is True
        assert body["ocr_confidence"] is not None
        assert isinstance(body["ocr_low_pages"], list)
        assert body["chunks"] and body["chunks"] > 0

        # chunk 表里应有页码（单页 PDF → 第 1 页）
        from app.db.session import SessionLocal
        from app.models.chunk import Chunk
        from app.models.document import DocVersion

        db = SessionLocal()
        try:
            version = db.get(DocVersion, body["current_version_id"])
            rows = db.query(Chunk).filter(Chunk.doc_version_id == version.id).all()
            assert rows and all(r.page_no == 1 for r in rows)
            assert rows[0].ocr_confidence is not None
        finally:
            db.close()

    def test_text_layer_pdf_skips_ocr(self):
        """电子版 PDF（有文本层）不应走 OCR，避免无谓的性能开销。"""
        import pymupdf

        pdf = pymupdf.open()
        page = pdf.new_page()
        page.insert_text((72, 72), "ELECTRONIC INVOICE WITH TEXT LAYER 2026", fontsize=12)
        data = pdf.tobytes()
        pdf.close()

        result = parse_file("e_invoice.pdf", data)
        assert result.ok
        assert result.ocr_used is False
        assert result.page_count == 1


class TestPageMapping:
    def test_page_spans_and_chunk_page(self):
        text = "第一页内容：报关单填写规范说明\n" + PAGE_SEPARATOR + "\n第二页内容：原产地证签发要求"
        spans = page_spans(text)
        assert len(spans) == 2

        pieces = split_text(text, max_chars=20, overlap=5, page_spans=spans)
        assert pieces
        assert {p["page_no"] for p in pieces} == {1, 2}

    def test_no_separator_means_no_page(self):
        assert page_spans("普通文本没有换页符") == []
        assert all(p["page_no"] is None for p in split_text("第一段内容\n\n第二段内容"))

    def test_never_merge_across_pages(self):
        """跨页不合并：否则引用页码只能标第一页，定位会错。"""
        text = f"第一页短句\n{PAGE_SEPARATOR}\n第二页短句"
        pieces = split_text(text, max_chars=500, page_spans=page_spans(text))
        assert [p["page_no"] for p in pieces] == [1, 2]


class TestReranker:
    def test_query_units_multilingual(self):
        units = query_units("不锈钢法兰 DN50 的材质是什么")
        assert "法兰" in units
        assert "dn50" in units

    def test_heuristic_prefers_relevant(self):
        class Dummy:
            def __init__(self, content: str) -> None:
                self.content = content
                self.rerank_score = None

        hits = [
            Dummy("商业发票须标注币种与汇率基准日"),
            Dummy("不锈钢法兰 DN50 材质 304 压力 1.6MPa"),
            Dummy("原产地证由贸促会签发"),
        ]
        ranked = HeuristicReranker().rerank("不锈钢法兰的材质", hits, top_k=2)
        assert len(ranked) == 2
        assert "法兰" in ranked[0].content
        assert ranked[0].rerank_score is not None

    def test_retrieval_reports_rerank(self, client, admin_headers, kb_a_id):
        """检索结果应带上重排信息（测试环境用启发式重排）。"""
        client.post(
            "/api/v1/documents/upload",
            headers=admin_headers,
            data={"kb_id": str(kb_a_id), "doc_type": "spec", "languages": "zh"},
            files={"file": ("valve.txt", "不锈钢法兰 DN50 材质304".encode(), "text/plain")},
        )
        resp = client.post(
            "/api/v1/debug/retrieval",
            headers=admin_headers,
            json={"query": "不锈钢法兰材质", "top_k": 3},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["rerank"]["applied"] in (True, False)  # 开关可配，但字段必须在
        assert "provider" in body["rerank"]


class TestSystemCapabilities:
    def test_capabilities(self, client, admin_headers):
        resp = client.get("/api/v1/system/capabilities", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["embedder"]["provider"]
        assert "ocr" in body and "reranker" in body
        assert body["vector_store"]["collection"]

    def test_capabilities_requires_auth(self, client):
        assert client.get("/api/v1/system/capabilities").status_code == 401


@pytest.mark.skipif(
    os.environ.get("RUN_MODEL_TESTS") != "1",
    reason="真实语义模型需下载数 GB 模型，设置 RUN_MODEL_TESTS=1 才运行",
)
class TestRealModel:
    def test_bge_m3_semantic_similarity(self):
        from app.services.embedding import SentenceTransformerEmbedder

        embedder = SentenceTransformerEmbedder()
        assert embedder.dim == 1024, f"bge-m3 应为 1024 维，实际 {embedder.dim}"

        def cosine(a, b):
            return sum(x * y for x, y in zip(a, b))

        vecs = embedder.embed(["不锈钢法兰的材质是什么", "法兰用什么材料制造", "今天天气很好"])
        assert cosine(vecs[0], vecs[1]) > cosine(vecs[0], vecs[2])
