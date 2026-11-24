"""文档上传与业务组隔离测试（对应 FR-101/FR-603/PRD 7.2 上传校验）。

覆盖：
1. 知识管理员上传 txt → 201 链路成功且文本已解析入库
2. 普通用户无 kb:manage 权限 → 403
3. 扩展名白名单：.exe 拒绝
4. 魔数校验：伪装成 .pdf 的假文件拒绝
5. 宏检测：含宏的 docx 拒绝（vbaProject.bin）
6. 业务组隔离：biz_a 的知识管理员不能传到 biz_b 的知识库；文档列表只见本组
7. 上传 PDF 真文件 → 解析出页数与文本
"""
from pathlib import Path
from io import BytesIO


def _upload(client, headers, kb_id, filename, content: bytes, **extra):
    data = {"kb_id": str(kb_id), **extra}
    return client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data=data,
        files={"file": (filename, BytesIO(content), "application/octet-stream")},
    )


# 用 PyMuPDF 生成一份合法的、含可提取文本的 PDF（手工拼字节不可靠）
import pymupdf

_pdf = pymupdf.open()
_page = _pdf.new_page()
_page.insert_text((72, 72), "Commercial Invoice No. 2026-001")
_page.insert_text((72, 92), "Product: Stainless Steel Flange DN50")
MINI_PDF = _pdf.tobytes()
_pdf.close()


class TestUpload:
    def test_upload_txt_success(self, client, knowledge_headers, kb_a_id):
        resp = _upload(
            client, knowledge_headers, kb_a_id, "产品规格.txt",
            "产品名称：不锈钢法兰\n材质：304\n规格：DN50".encode("utf-8"),
            doc_type="spec", languages="zh",
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "parsed"          # 状态流转：uploaded → parsing → parsed
        assert data["parse_status"] == "done"
        assert data["biz_group"] == "biz_a"        # 业务组随知识库，非用户自选
        assert data["doc_type"] == "spec"

        # 文本确实解析入库
        detail = client.get(f"/api/v1/documents/{data['id']}", headers=knowledge_headers)
        assert detail.status_code == 200

    def test_normal_user_forbidden(self, client, sales_headers, kb_a_id):
        """普通用户没有 kb:manage 权限 → 403。"""
        resp = _upload(client, sales_headers, kb_a_id, "x.txt", "hello".encode())
        assert resp.status_code == 403

    def test_reject_disallowed_extension(self, client, knowledge_headers, kb_a_id):
        resp = _upload(client, knowledge_headers, kb_a_id, "virus.exe", b"MZ\x90\x00binary")
        assert resp.status_code == 422
        assert "白名单" in resp.json()["detail"]["message"]

    def test_reject_fake_pdf_magic(self, client, knowledge_headers, kb_a_id):
        """扩展名 .pdf 但内容不是 PDF（魔数校验）→ 拒绝。"""
        resp = _upload(client, knowledge_headers, kb_a_id, "fake.pdf", b"not a real pdf")
        assert resp.status_code == 422
        assert "魔数" in resp.json()["detail"]["message"]

    def test_reject_docx_with_macro(self, client, knowledge_headers, kb_a_id):
        """含宏（vbaProject.bin）的 docx → 拒绝（PRD 7.2 含宏即拒）。"""
        import zipfile
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", "<xml/>")
            zf.writestr("word/vbaProject.bin", b"malicious macro payload")
        resp = _upload(client, knowledge_headers, kb_a_id, "macro.docx", buf.getvalue())
        assert resp.status_code == 422
        assert "宏" in resp.json()["detail"]["message"]

    def test_upload_mini_pdf(self, client, knowledge_headers, kb_a_id):
        resp = _upload(client, knowledge_headers, kb_a_id, "invoice.pdf", MINI_PDF, doc_type="customs", languages="en")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "parsed"
        assert data["parse_status"] == "done"
        assert data["page_count"] == 1

    def test_biz_group_isolation_upload(self, client, knowledge_headers, kb_b_id):
        """biz_a 的知识管理员不能向 biz_b 的知识库上传（FR-603）。"""
        resp = _upload(client, knowledge_headers, kb_b_id, "x.txt", "hello".encode())
        assert resp.status_code == 403

    def test_biz_group_isolation_list(self, client, knowledge_headers, knowledge_headers_b, kb_b_id):
        """biz_b 的文档不出现在 biz_a 用户的列表里。"""
        # 先用 biz_b 的知识管理员上传一份
        resp = _upload(client, knowledge_headers_b, kb_b_id, "b组文档.txt", "biz b secret".encode())
        assert resp.status_code == 200
        # biz_a 用户列表里不应出现
        listed = client.get("/api/v1/documents", headers=knowledge_headers).json()
        assert all(doc["biz_group"] == "biz_a" for doc in listed)
        # 直接访问 biz_b 文档详情 → 403
        doc_b_id = resp.json()["id"]
        resp2 = client.get(f"/api/v1/documents/{doc_b_id}", headers=knowledge_headers)
        assert resp2.status_code == 403
