"""OCR 服务：把扫描件（图片型 PDF）识别成文字（对应 PRD FR-101 / FR-104）。

给新手的说明：
- 什么样的 PDF 需要 OCR？"扫描件"——整页是一张图，选中文字选不到（文本层为空）。
  电子版 PDF（Word 导出）本身带文字层，直接提取即可，不需要 OCR。
- 本模块用 RapidOCR（ONNXRuntime 版）：纯 pip 安装、自带中英文模型、CPU 即可跑，
  比 PaddleOCR 轻得多（不需要 paddlepaddle 大依赖），适合本地开发与中小规模部署。
  如需更高精度可换 PaddleOCR / 商业 OCR，只要实现同一个 recognize() 接口即可（可插拔）。
- 置信度（FR-104）：OCR 结果自带每行得分，取加权平均。低于阈值（默认 0.85）
  的页面会被标记，文档详情里提示"需人工抽检"——防止识别错字被当作事实入库。
"""
from __future__ import annotations

import io
import logging
import threading
from dataclasses import dataclass, field

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class OcrResult:
    """单页 OCR 结果。"""

    text: str = ""
    confidence: float = 0.0
    ok: bool = False
    error: str | None = None


class BaseOcrEngine:
    name = "base"

    def recognize(self, image_bytes: bytes) -> OcrResult:
        raise NotImplementedError


class RapidOcrEngine(BaseOcrEngine):
    """RapidOCR（PP-OCR 模型的 ONNX 版本）。首次调用时加载模型（约 1~2 秒）。"""

    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR  # 延迟导入：未装依赖时给清晰报错

        self._engine = RapidOCR()

    def recognize(self, image_bytes: bytes) -> OcrResult:
        import numpy as np
        from PIL import Image

        try:
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            # RapidOCR 底层是 OpenCV，按 BGR 传入更稳（RGB → BGR 只是通道反转）
            array = np.asarray(image)[:, :, ::-1]
            raw, _elapse = self._engine(array)
        except Exception as exc:  # 单页失败不炸整篇文档
            logger.warning("OCR 单页识别失败：%s", exc)
            return OcrResult(error=f"{type(exc).__name__}: {exc}")

        if not raw:
            return OcrResult(text="", confidence=0.0, ok=False, error="未识别到文字")

        lines: list[str] = []
        scores: list[float] = []
        for item in raw:
            # RapidOCR 返回 [[box, text, score], ...]
            if len(item) < 3:
                continue
            text, score = str(item[1]).strip(), float(item[2])
            if not text:
                continue
            # 表格/多列里同一行会被切成多块：按长度加权提取文本，保持阅读顺序
            lines.append(text)
            scores.append(score * max(len(text), 1))

        if not lines:
            return OcrResult(text="", confidence=0.0, ok=False, error="未识别到文字")

        total_chars = sum(max(len(line), 1) for line in lines)
        confidence = sum(scores) / total_chars if total_chars else 0.0
        return OcrResult(text="\n".join(lines), confidence=round(confidence, 4), ok=True)


_engine: BaseOcrEngine | None = None
_engine_failed = False
_lock = threading.Lock()


def get_ocr_engine() -> BaseOcrEngine | None:
    """返回 OCR 引擎单例；不可用时返回 None（调用方按"无 OCR 能力"降级）。"""
    global _engine, _engine_failed
    if _engine is not None:
        return _engine
    if _engine_failed:
        return None
    with _lock:
        if _engine is not None:
            return _engine
        try:
            _engine = RapidOcrEngine()
            logger.info("OCR 引擎就绪：%s", _engine.name)
            return _engine
        except Exception as exc:
            logger.warning("OCR 引擎不可用（%s），扫描件将标记为需人工处理", exc)
            _engine_failed = True
            return None


def ocr_available() -> bool:
    return get_ocr_engine() is not None


def ocr_state() -> dict:
    """OCR 状态（probe=True 时才真正加载引擎，避免每次查询都付启动成本）。"""
    settings = get_settings()
    return {
        "enabled": settings.ocr_enabled,
        "engine": _engine.name if _engine is not None else ("unavailable" if _engine_failed else "not-loaded"),
        "loaded": _engine is not None,
        "dpi": settings.ocr_dpi,
        "min_confidence": settings.ocr_min_confidence,
        "min_text_chars": settings.ocr_min_text_chars,
    }


def reset_ocr_engine() -> None:
    """测试辅助。"""
    global _engine, _engine_failed
    _engine, _engine_failed = None, False


def recognize_image(image_bytes: bytes) -> OcrResult:
    engine = get_ocr_engine()
    if engine is None:
        return OcrResult(error="OCR 引擎不可用")
    return engine.recognize(image_bytes)


def render_pdf_page_png(page, dpi: int = 200) -> bytes:
    """把 PDF 的某一页渲染成 PNG 字节（供 OCR）。page 为 pymupdf.Page。"""
    import pymupdf

    zoom = dpi / 72.0  # PDF 默认 72 DPI，按目标 DPI 放大
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return pixmap.tobytes("png")


@dataclass
class PdfOcrOutcome:
    """整篇 PDF 的 OCR 汇总（供解析器填充文档级字段）。"""

    pages: list[str] = field(default_factory=list)
    page_confidences: list[float | None] = field(default_factory=list)
    ocr_page_numbers: list[int] = field(default_factory=list)
    low_confidence_pages: list[int] = field(default_factory=list)

    @property
    def used(self) -> bool:
        return bool(self.ocr_page_numbers)

    @property
    def mean_confidence(self) -> float | None:
        values = [c for c in self.page_confidences if c is not None]
        return round(sum(values) / len(values), 4) if values else None
