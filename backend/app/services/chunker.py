"""文本切分器（对应 PRD chunk_strategy；M2 默认策略，后续按文档类型细化）。

给新手的说明：
- 策略：优先按自然段（空行）聚合成约 max_chars 的块；单段超长时按句子/硬切兜底；
  相邻块之间保留 overlap 个字符的重叠，避免答案恰好被切在块边界上丢失上下文。
- 语种探测用 Unicode 字符区间启发式（中/英/西/阿），不需要额外模型。
"""
import re
import unicodedata

DEFAULT_MAX_CHARS = 500
DEFAULT_OVERLAP = 50

# 语种字符区间
_ZH = re.compile(r"[\u4e00-\u9fff]")
_AR = re.compile(r"[\u0600-\u06ff]")
_ES = re.compile(r"[ñ¿¡áéíóúü]", re.IGNORECASE)


def detect_language(text: str) -> str:
    """轻量语种探测：按特征字符占比。返回 zh/en/es/ar/mixed。"""
    if not text.strip():
        return "en"
    total = max(len(text), 1)
    zh = len(_ZH.findall(text)) / total
    ar = len(_AR.findall(text)) / total
    if zh > 0.15:
        return "zh" if zh > ar * 3 else "mixed"
    if ar > 0.15:
        return "ar"
    if _ES.search(text):
        return "es"
    return "en"


def _hard_split(segment: str, max_chars: int, overlap: int) -> list[str]:
    """单段超长时的兜底硬切（带重叠）。"""
    pieces = []
    start = 0
    while start < len(segment):
        pieces.append(segment[start : start + max_chars])
        start += max_chars - overlap
    return pieces


def split_text(
    text: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
    page_spans: list[tuple[int, int, int]] | None = None,
) -> list[dict]:
    """把纯文本切分为块列表。

    page_spans：[(起始偏移, 结束偏移, 页码)]，由 parser.page_spans() 生成；
    传入后每个块会带上 page_no（引用可显示"第 N 页"）。

    返回：[{"chunk_index": 0, "content": "...", "language": "zh", "page_no": 1}, ...]
    保证块内容非空；空文本返回 []。
    """
    assert overlap < max_chars, "overlap 必须小于 max_chars，否则会死循环"
    raw = text
    lead = len(raw) - len(raw.lstrip())  # strip 掉的前导空白长度，用于还原偏移
    text = raw.strip()
    if not text:
        return []

    # 1) 按自然段切分，同时记录每段在全文中的起始偏移（页码定位需要）
    blocks: list[tuple[int, str]] = []
    pos = 0
    for match in re.finditer(r"\n\s*\n", text):
        block = text[pos : match.start()]
        stripped = block.strip()
        if stripped:
            local = pos + (len(block) - len(block.lstrip()))
            blocks.append((local + lead, stripped))
        pos = match.end()
    tail = text[pos:]
    if tail.strip():
        local = pos + (len(tail) - len(tail.lstrip()))
        blocks.append((local + lead, tail.strip()))

    # 2) 聚合成块（保留起始偏移；跨页处强制断开，保证引用页码准确）
    pieces: list[tuple[int, str]] = []
    buf = ""
    buf_start = 0
    buf_page: int | None = None
    spans = page_spans or []
    for start, para in blocks:
        page = _page_of(start, spans)
        if buf and page is not None and buf_page is not None and page != buf_page:
            pieces.append((buf_start, buf))  # 不跨页合并：否则引用只能标第一页
            buf = ""
        if len(para) > max_chars:
            if buf:
                pieces.append((buf_start, buf))
                buf = ""
            for i, part in enumerate(_hard_split(para, max_chars, overlap)):
                pieces.append((start + i * (max_chars - overlap), part))
            continue
        if len(buf) + len(para) + 1 <= max_chars:
            if not buf:
                buf_start, buf_page = start, page
            buf = f"{buf}\n{para}" if buf else para
        else:
            if buf:
                pieces.append((buf_start, buf))
            buf, buf_start, buf_page = para, start, page
    if buf:
        pieces.append((buf_start, buf))

    # 3) 语种 + 页码标注
    return [
        {
            "chunk_index": i,
            "content": content.strip(),
            "language": detect_language(content),
            "page_no": _page_of(start, spans),
        }
        for i, (start, content) in enumerate(pieces)
        if content.strip()
    ]


def _page_of(offset: int, spans: list[tuple[int, int, int]]) -> int | None:
    """按字符偏移反查页码（spans 按起始偏移升序且首尾相接）。"""
    if not spans:
        return None
    page: int | None = None
    for start, _end, page_no in spans:
        if offset < start:
            break
        page = page_no
    return page

