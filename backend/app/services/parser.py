"""多格式文本解析服务（对应 PRD FR-101 / FR-102 / FR-104 / 7.2）。

给新手的说明：
- "解析"= 把 PDF/Word/Excel 里的文字提取成纯文本，供后续切分和嵌入。
- 支持：txt / md（纯文本）、pdf（电子版取文本层 + 扫描页自动 OCR）、
  docx（python-docx）、xlsx（openpyxl，逐 sheet 拼接）。
- PDF 是"逐页判断"的：某页文本层字数 ≥ ocr_min_text_chars 就当作电子页直接取文字；
  否则当作扫描页，渲染成图片交给 OCR（PRD 假设扫描件约占 60%）。
  这样"电子页 + 扫描页混排"的文档也能正确解析。
- 页与页之间用换页符 \\f 分隔：后续切分时据此计算每个 chunk 落在第几页，
  引用就能显示"第 12 页"（FR-206 引用可定位）。
- PyMuPDF 只取静态文本层，不执行 PDF 内嵌 JS（7.2 投毒防范：pdf.js 逐字符换行混入文本）。
"""
import io
import logging

from app.config import get_settings
from app.services.ocr import PdfOcrOutcome, ocr_available, recognize_image, render_pdf_page_png

logger = logging.getLogger(__name__)

# 支持的扩展名 → 解析器（PRD 7.2 上传校验白名单思想，含魔数校验见 upload 接口）
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".xlsx"}

PAGE_SEPARATOR = "\f"  # 换页符：页边界标记（也天然是段落分隔）


class ParseResult:
    def __init__(self) -> None:
        self.text: str = ""
        self.page_count: int | None = None
        self.error: str | None = None
        # ---- OCR 相关（FR-104：低置信度需人工抽检）----
        self.ocr_used: bool = False
        self.ocr_confidence: float | None = None
        self.ocr_pages: list[int] = []
        self.low_confidence_pages: list[int] = []
        self.ocr_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


def _parse_pdf(data: bytes) -> ParseResult:
    import pymupdf  # fitz

    settings = get_settings()
    result = ParseResult()
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        result.page_count = len(pdf)
        pages: list[str] = []
        ocr_outcome = PdfOcrOutcome()

        for page_no, page in enumerate(pdf, start=1):
            text = page.get_text("text").strip()
            if len(text) >= settings.ocr_min_text_chars:
                pages.append(text)
                continue

            # ---- 扫描页：渲染成图 → OCR ----
            if not settings.ocr_enabled:
                pages.append(text)
                continue
            if page_no > settings.ocr_max_pages:
                result.ocr_error = f"超过 OCR 页数上限 {settings.ocr_max_pages}，后续页未识别"
                pages.append(text)
                continue
            if not ocr_available():
                result.ocr_error = "OCR 引擎不可用，扫描页未识别"
                pages.append(text)
                continue

            ocr = recognize_image(render_pdf_page_png(page, dpi=settings.ocr_dpi))
            if ocr.ok:
                pages.append(ocr.text)
                ocr_outcome.ocr_page_numbers.append(page_no)
                ocr_outcome.page_confidences.append(ocr.confidence)
                if ocr.confidence < settings.ocr_min_confidence:
                    ocr_outcome.low_confidence_pages.append(page_no)
                logger.info("OCR 完成 page=%s chars=%s conf=%.3f", page_no, len(ocr.text), ocr.confidence)
            else:
                # 识别失败：保留文本层（通常为空），记错误原因供人工跟进
                pages.append(text)
                result.ocr_error = ocr.error or "OCR 未识别到文字"
                logger.warning("OCR 未识别出文字 page=%s reason=%s", page_no, ocr.error)

        # 注意：不要在 with 块内调用 pdf.close()，上下文管理器会自动关闭
        result.text = f"\n{PAGE_SEPARATOR}\n".join(p.strip() for p in pages)

    if ocr_outcome.used:
        result.ocr_used = True
        result.ocr_confidence = ocr_outcome.mean_confidence
        result.ocr_pages = ocr_outcome.ocr_page_numbers
        result.low_confidence_pages = ocr_outcome.low_confidence_pages
    return result


def _parse_docx(data: bytes) -> ParseResult:
    import docx  # python-docx

    result = ParseResult()
    document = docx.Document(io.BytesIO(data))
    parts: list[str] = [p.text for p in document.paragraphs]
    for table in document.tables:  # 表格文本也要（产品规格常在表格里）
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    result.text = "\n".join(parts)
    return result


def _parse_xlsx(data: bytes) -> ParseResult:
    import openpyxl

    result = ParseResult()
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines: list[str] = []
    for sheet in wb.worksheets:
        lines.append(f"## Sheet: {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                lines.append(" | ".join(cells))
    wb.close()
    result.text = "\n".join(lines)
    return result


def _parse_plaintext(data: bytes) -> ParseResult:
    result = ParseResult()
    for encoding in ("utf-8", "gbk"):  # 国内环境常有 GBK 老文件
        try:
            result.text = data.decode(encoding)
            return result
        except UnicodeDecodeError:
            continue
    result.error = "无法识别文本编码（非 UTF-8/GBK）"
    return result


def parse_file(filename: str, data: bytes) -> ParseResult:
    """按扩展名路由到对应解析器。未知扩展名直接拒绝（白名单，PRD 7.2）。"""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED_EXTENSIONS:
        r = ParseResult()
        r.error = f"不支持的文件类型：{ext or '无扩展名'}（白名单：{', '.join(sorted(SUPPORTED_EXTENSIONS))}）"
        return r
    try:
        if ext == ".pdf":
            return _parse_pdf(data)
        if ext == ".docx":
            return _parse_docx(data)
        if ext == ".xlsx":
            return _parse_xlsx(data)
        return _parse_plaintext(data)
    except Exception as exc:  # 解析器内部异常统一兜底，不让流水线崩溃（FR-216 精神）
        logger.exception("解析失败 file=%s", filename)
        r = ParseResult()
        r.error = f"解析异常：{type(exc).__name__}: {exc}"
        return r


def page_spans(text: str, page_count: int | None = None) -> list[tuple[int, int, int]]:
    """根据换页符计算 [(起始偏移, 结束偏移, 页码)]，供切分时定位页码。

    - 多页 PDF：按 \\f 切分累加偏移。
    - 单页 PDF：没有换页符，但已知 page_count=1，整篇就是第 1 页。
    - txt/docx/xlsx：无换页符且无页数 → 返回空列表，chunk 页码留空。
    """
    if PAGE_SEPARATOR not in text:
        if page_count == 1 and text.strip():
            return [(0, len(text), 1)]
        return []
    spans: list[tuple[int, int, int]] = []
    start = 0
    for page_no, segment in enumerate(text.split(PAGE_SEPARATOR), start=1):
        end = start + len(segment)
        spans.append((start, end, page_no))
        start = end + len(PAGE_SEPARATOR)
    return spans
