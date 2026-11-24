# 12 补齐两处功能缺口：Query 改写 + 切分分派

> 本文是实施方案，不含已生效的代码改动。经确认后按「执行计划」分批落地。
> 前置事实均已在真机核对（行号对应当前 `main` 工作区）。

## 0. 总览

| 项 | Query 改写 | 切分分派 |
| --- | --- | --- |
| 解决什么 | 多轮追问变成「伪多轮」；口语/复合问题召回低 | DOCX/XLSX 段落丢失；全部文档一刀切 500/50 |
| 改动点 | 新增 1 文件 + `chat.py` 两处插桩 | `parser.py` 1 行 + `chunker.py` 1 函数 + `pipeline.py` 3 行 |
| 存量数据影响 | 无 | **需重建索引**（28 篇） |
| 风险 | 低（可开关、可回退原句） | 中（改变已有分片与评测基线） |
| 预估 | 2 人日 | 2 人日 |

**顺序建议**：先改写、后切分。改写独立、可开关、不动存量；切分会动 28 篇文档的索引，必须在有评测基线的前提下做。

---

## 1. Query 改写

### 1.1 现状（已核实）

`chat.py:349` 是检索入口，直接吃原始 query：

```python
result = retrieve(db, user, query, top_k=TOP_K, crosslingual=True, lf=ret_span)
```

`build_messages(query, refs, answer_language)`（`chat.py:92`）也只收当前这一句，历史一条不带。`_get_or_create_session`（`chat.py:131`）只负责建/取会话，不读消息。

所以「先问出口沙特要什么认证，再问那费用大概多少」时，第二句会拿「费用」两个字去检索，必然跑偏。

### 1.2 关键约束：不能全量调 LLM

- 当前整条链路 15.8s，首字 12.96s，用户对延迟敏感（已有 `RagTTFTSlow` 告警）。
- `chat_once` 的 HTTP 超时是 **20s**（`llm.py:345`），直接用会把最坏情况从 16s 拉到 36s。

**结论：只在「有历史 + 命中启发式」时才调 LLM，且外层自己加 2s 超时。首轮问题零延迟。**

### 1.3 三层设计，按代价从低到高

| 层 | 手段 | 触发条件 | 额外延迟 | 批次 |
| --- | --- | --- | --- | --- |
| L1 指代消解 | LLM 改写 | 有历史 且（含指代词 或 长度 ≤ 12） | 0.8–1.5s | **第一批** |
| L2 复合拆解 | 同一 LLM 调用内兼顾 | 长度 ≥ 40 且含并列词且是问句 | 同上（合并） | 第一批 |
| L3 术语归一 | 规则词典追加同义词 | 命中词表 | 0 | 第二批 |

L1 和 L2 用**同一次调用**完成，不额外花钱。

L3 刻意不用 LLM：把「CO证」替换成「原产地证」会破坏原文的 BM25 命中（原文里就写着 CO），所以做法是**追加**而不是替换 —— `CO证怎么办` → `CO证怎么办（原产地证 Certificate of Origin）`。零延迟、可解释、可回滚。

### 1.4 新增文件：`backend/app/services/query_understand.py`

```python
"""查询理解与改写（12-1）。

三层按代价从低到高：术语归一（规则，零延迟）→ 指代消解（LLM）→ 复合拆解（同一次 LLM 调用）。

三条设计红线：
1. 首轮问题不调 LLM —— 没有指代风险，调了只是白加 1s 延迟。
2. 改写失败一律回退原 query —— 改写是增强项，不能成为新的故障点。
3. 每次改写都上报 trace —— 否则分不清"改写导致偏题"还是"检索本身弱"。
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

from sqlalchemy.orm import Session

from app.services.llm import get_llm_client

logger = logging.getLogger(__name__)

REWRITE_TIMEOUT = 2.0          # 秒；超过即用原 query，不阻塞主链路
HISTORY_TURNS = 4              # 取最近 4 条消息（约 2 轮）
MAX_QUERY_AFTER = 300          # 改写结果超长视为失败（防模型跑飞）

# 指代词：出现即认为当前句可能省略了主语
_ANAPHORA = ("它", "他", "这个", "那个", "上述", "该", "其", "这些", "那些", "这几种", "那几种")
# 追问信号：短句里出现这些词，多半是接着上一轮问
_FOLLOW = ("还有", "呢", "那么", "那", "如果", "要是", "分别")
# 并列结构：复合问题的典型长相
_CONJ = ("和", "以及", "与", "还有", "分别")

REWRITE_PROMPT = """你是检索查询改写器。根据对话历史，把用户最新一句话改写成一条不依赖上下文、可直接用于检索的完整查询。
规则：
1. 补全指代（它/这个/上述/该 → 具体对象），保留原意，不得新增事实或臆测。
2. 若一句话里包含多个并列子问题，用「；」分隔，不要拆成多条查询。
3. 口语、缩写、错别字归一到书面术语。
4. 若原句本身已经完整，原样返回。
只输出改写后的查询，不要解释、不要引号、不要「改写后：」之类前缀。"""

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="qrw")


def _is_question(q: str) -> bool:
    return q.endswith(("？", "?")) or any(
        k in q for k in ("怎么", "如何", "什么", "多少", "哪", "是否", "能不能", "怎么办", "为什么")
    )


def should_rewrite(query: str, history: list[dict]) -> tuple[bool, str]:
    """启发式判断要不要调 LLM。返回 (是否改写, 原因)。原因会写进 trace，便于事后调参。"""
    q = query.strip()
    if len(q) < 4:
        return False, "too_short"
    if history and (any(k in q for k in _ANAPHORA) or len(q) <= 12 or any(k in q for k in _FOLLOW)):
        return True, "anaphora"
    if len(q) >= 40 and _is_question(q) and sum(1 for k in _CONJ if k in q) >= 1:
        return True, "compound"
    return False, "skip"


def _clean(out: str, original: str) -> str:
    """清洗模型输出：去引号/前缀/多余空白；异常长度或与原句相同则视为无效。"""
    s = (out or "").strip().strip("「」\"'“”")
    for prefix in ("改写后：", "改写：", "查询：", "检索查询："):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    s = re.sub(r"\s+", " ", s).strip()
    if not s or len(s) > MAX_QUERY_AFTER or s == original.strip():
        return ""
    return s


def _call_with_timeout(fn, timeout: float):
    future = _executor.submit(fn)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeout:
        future.cancel()          # 线程仍会跑完（HTTP 层 20s 兜底），但不再阻塞主链路
        return None
    except Exception:            # noqa: BLE001
        logger.warning("查询改写调用失败，回退原 query", exc_info=True)
        return None


def rewrite_query(db: Session, query: str, history: list[dict], lf=None) -> dict:
    """返回 {query, original, rewritten, reason, degraded, took_ms}。

    query 字段永远是"可以直接拿去检索的那一句"——调用方无脑用即可，
    不需要自己判断改写是否成功。这是本函数对外的唯一契约。
    """
    base = {"query": query, "original": query, "rewritten": False,
            "reason": "skip", "degraded": False, "took_ms": 0}
    need, reason = should_rewrite(query, history)
    if not need:
        base["reason"] = reason
        return base

    client = get_llm_client(db)
    if not client.available:
        base.update(reason="no_llm", degraded=True)
        return base

    ctx = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}：{(m['content'] or '')[:200]}"
        for m in history[-HISTORY_TURNS:]
    )
    messages = [
        {"role": "system", "content": REWRITE_PROMPT},
        {"role": "user", "content": f"【对话历史】\n{ctx}\n\n【最新一句】\n{query}"},
    ]

    t0 = time.perf_counter()
    out = _call_with_timeout(
        lambda: client.chat_once(messages, temperature=0.0, max_tokens=120, db=db),
        REWRITE_TIMEOUT,
    )
    took = int((time.perf_counter() - t0) * 1000)
    cleaned = _clean(out, query) if out else ""
    if not cleaned:
        base.update(reason=f"{reason}:timeout" if out is None else f"{reason}:invalid",
                    degraded=True, took_ms=took)
        return base

    base.update(query=cleaned, rewritten=True, reason=reason, took_ms=took)
    return base
```

### 1.5 `chat.py` 改动（两处）

**改动 A：取历史（必须放在写当前消息之前）**

`chat.py:205` 现在是：

```python
session = _get_or_create_session(db, user_id, session_id, query)
db.add(Message(session_id=session.id, role=ROLE_USER, content=query, request_id=request_id))
```

中间插一段，**顺序不能反** —— 一旦当前这句先 add，查历史会把自己当成上一轮：

```python
session = _get_or_create_session(db, user_id, session_id, query)
history = _recent_history(db, session.id, limit=HISTORY_TURNS)   # 必须在 add 之前
db.add(Message(session_id=session.id, role=ROLE_USER, content=query, request_id=request_id))
```

新增辅助函数（放 `_get_or_create_session` 后面）：

```python
def _recent_history(db: Session, session_id: int, limit: int = 4) -> list[dict]:
    """取最近若干条消息（旧→新）。排除 system 类，控制单条长度防 prompt 膨胀。"""
    rows = (db.query(Message)
              .filter(Message.session_id == session_id, Message.role.in_((ROLE_USER, ROLE_ASSISTANT)))
              .order_by(Message.id.desc()).limit(limit).all())
    return [{"role": r.role, "content": (r.content or "")[:300]} for r in reversed(rows)]
```

**改动 B：改写 + 检索（替换 `chat.py:343-349`）**

```python
# ---- 知识库提问：查询理解 → 检索 ----
with lf.span_cm("query-rewrite") as qr_span:
    qr = rewrite_query(db, query, history)
    qr_span.update(
        input={"query": query, "history_turns": len(history)},
        output={"query": qr["query"], "rewritten": qr["rewritten"]},
        metadata={"reason": qr["reason"], "degraded": qr["degraded"], "took_ms": qr["took_ms"]},
    )
metrics.record_query_rewrite(qr["reason"], qr["rewritten"], qr["took_ms"])
search_query = qr["query"]

yield "stage", {"phase": "retrieval", "session_id": session.id, "message": "正在检索知识库…"}
metrics.enter("retrieval")
with lf.span_cm("retrieval") as ret_span:
    result = retrieve(db, user, search_query, top_k=TOP_K, crosslingual=True, lf=ret_span)
    ret_span.update(input={"query": search_query, "original_query": query, ...}, ...)
```

注意 `ret_span.update(input=...)` 里要同时保留 `original_query`，否则在瀑布图上只看到改写后的句子，会误判。

**生成阶段仍用原句**：`build_messages(query, refs, answer_language)` 不改 —— 用户看到的是自己的问法，模型按原句组织答案，改写只服务于检索。这是个刻意的选择。

**`meta` 事件补一个字段**，供前端展示（`chat.py:388` 的 `yield "meta"`）：

```python
"rewritten_query": qr["query"] if qr["rewritten"] else None,
```

### 1.6 指标与配置

`metrics.py` 新增：

```python
_QUERY_REWRITE = Counter("rag_query_rewrite_total", "查询改写次数", ["result", "rewritten"])
_QUERY_REWRITE_MS = Histogram("rag_query_rewrite_duration_seconds", "查询改写耗时",
                              buckets=(0.2, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0))
```

`config.py` 新增三项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `query_rewrite_enabled` | `True` | 总开关，关掉后 `rewrite_query` 直接返回原句 |
| `query_rewrite_timeout` | `2.0` | 秒，超过即降级 |
| `query_rewrite_history_turns` | `4` | 取几条历史消息 |

**建议 `query_rewrite_enabled` 初始设为 `False` 上线**，观察一周 trace 里的改写质量后再开。理由：改写错了比不改更糟，而且不容易被用户察觉 —— 他只会觉得「答得不对」。

### 1.7 前端

`admin.js` 的链路模块不用改（新 span 会自动出现在瀑布图里）。

`index.html` 的问答区建议加一行小字，只在改写发生时显示：

```
已按上下文理解为：原产地证需要哪些材料？
```

不加这行的话，用户看到答案跟自己的问法对不上，会以为是幻觉。

### 1.8 验证

三条对话，覆盖三种路径：

| # | 操作 | 预期 |
| --- | --- | --- |
| 1 | 首轮直接问「出口沙特要什么认证」 | trace 里 `query-rewrite` 的 `reason=skip`，`took_ms=0`，总耗时与改动前一致 |
| 2 | 追问「那费用大概多少」 | `reason=anaphora`，`rewritten=true`，改写结果含「沙特」「认证」等上文实体 |
| 3 | 问「原产地证和商业发票在报关时有什么区别」 | `reason=compound`，改写结果用「；」分隔成两个子问题 |

补充检查：
- `rag_query_rewrite_total{result}` 三个标签值都出现过
- 停掉 LLM Key（清空 `llm_config`），追问时 `degraded=true` 且**照常出答案**（回退原句）
- 回归 `pytest backend/tests/test_chat.py`，192 用例不得减少

### 1.9 回退

`query_rewrite_enabled=False` 一行配置即可关闭，无需回滚代码。彻底回退：删 `query_understand.py` + 还原 `chat.py` 两处。

---

## 2. 切分分派

### 2.1 三个已核实的缺陷

| # | 位置 | 问题 | 后果 |
| --- | --- | --- | --- |
| 1 | `parser.py:108` | `"\n".join(parts)`，Word 段落之间只有一个换行 | 空行正则 `\n\s*\n` 匹配不到，**整篇被当成一段**，按 500 硬切，段落边界全丢 |
| 2 | `parser.py:125` | xlsx 每行单 `\n` | 硬切把表格行劈成两半，型号参数被拆到两个块 |
| 3 | `pipeline.py:99` | `split_text(...)` 不传参数，全部落 500/50 | 规格书（表格）与合规条文（长段落）用同一套参数，前者块太大后者块太小 |

实测证据（上一轮已做）：

| 样本 | 解析后含空行 | 切分结果 |
| --- | --- | --- |
| Markdown 1628 字 | 是 | 5 块 440/225/500/200/300，边界在段间 |
| DOCX 5 段 984 字 | **否** | 3 块 500/500/84，第 3 段被腰斩 |
| XLSX 21 行 1125 字 | **否** | 3 块，块 2 首行是 `PN16 \| 水/蒸汽`（上一行后半截） |

### 2.2 改动 1：DOCX 段落分隔（`parser.py:104-108`）

```python
def _parse_docx(data: bytes) -> ParseResult:
    import docx

    result = ParseResult()
    document = docx.Document(io.BytesIO(data))
    # 只保留非空段落：Word 里连续空段落会产生 ""，join 后变成一堆多余换行
    parts: list[str] = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    result.text = "\n\n".join(parts)      # ← 唯一实质改动：单换行 → 空行
    return result
```

为什么安全：`\n\s*\n` 对连续多个空行是贪婪匹配（`\s*` 吃掉全部空白），不会产生空块。

### 2.3 改动 2：行感知硬切（`chunker.py:36-43`）

把 `_hard_split` 换成：

```python
def _hard_split(segment: str, max_chars: int, overlap: int, line_aware: bool = True) -> list[str]:
    """单段超长时的兜底硬切。line_aware=True 时切点对齐到换行，避免劈开表格行。

    对齐策略：
    - 切点：只在块的后半段找换行（`start + max_chars//2` 之后），防止切出过短的块。
    - 下一起点：重叠位置也回退到行首，避免下一块以半行开头。
    - 重叠量因此会比 overlap 略大（多出半行长度），这是可接受的代价。
    """
    if max_chars <= 0:
        return [segment]
    pieces: list[str] = []
    start, n = 0, len(segment)
    while start < n:
        end = min(start + max_chars, n)
        if line_aware and end < n:
            nl = segment.rfind("\n", start + max_chars // 2, end)
            if nl != -1:
                end = nl + 1
        pieces.append(segment[start:end])
        if end >= n:
            break
        nxt = end - overlap
        if line_aware and nxt > start:
            nl2 = segment.rfind("\n", start, nxt)
            if nl2 != -1:
                nxt = nl2 + 1
        start = max(nxt, start + 1)      # 兜底：保证 start 严格前进，不会死循环
    return pieces
```

`split_text` 签名加一个 `line_aware: bool = True` 并透传给 `_hard_split`。

对现有行为的影响：纯文本（Markdown）走的是段落聚合路径，只有单段超 500 才进 `_hard_split`；这类段落内通常没有换行，`rfind` 返回 -1，行为与现在完全一致。

### 2.4 改动 3：按 doc_type 分派（`pipeline.py:96-99`）

`meta` 在第 65 行已经查出来了，直接可用：

```python
# doc_type → (max_chars, overlap)。表格类块小、条款类块大，理由见文档 2.6
CHUNK_PROFILES: dict[str, tuple[int, int]] = {
    "spec":       (400, 60),   # 规格书：表格行短，块小提升命中精度
    "customs":    (600, 80),   # 单证：条款连续，块大保上下文
    "compliance": (600, 80),   # 合规文件：条文跨段落
    "policy":     (500, 50),
    "other":      (500, 50),
}
DEFAULT_PROFILE = (500, 50)

doc_type = (meta.doc_type if meta else "other") or "other"
max_chars, overlap = CHUNK_PROFILES.get(doc_type, DEFAULT_PROFILE)
strategy_tag = f"{doc_type}:{max_chars}/{overlap}"

spans = page_spans(version.raw_text, version.page_count)
pieces = split_text(version.raw_text, max_chars=max_chars, overlap=overlap, page_spans=spans)
version.chunk_strategy = strategy_tag          # ← 首次真正写入该字段
```

**不需要 Alembic 迁移**：`doc_version.chunk_strategy` 字段已存在（`models/document.py:64`），只是从来没人赋值。

### 2.5 存量数据怎么处理

改完后只对**新上传文档**生效。28 篇存量仍是旧参数切的，`chunk_strategy` 全是 `default`。

推荐流程（不要一键全量重建）：

1. 先挑 1 篇 `spec`（表格最多）与 1 篇 `compliance`，调 `POST /api/v1/kb/{id}/reindex` 重建，在知识库页点「分片」核对块数与边界。
2. 跑一次 `retrieval_zh` 全量评测（55 条），记录 Recall/MRR/nDCG 作为**改动后基线**。
3. 与改动前基线对比。Recall 下降超过 0.03 就停止，回到第 1 步查是块太大还是太小。
4. 通过后再批量重建其余文档。

`chunk_strategy` 从此能回答「这篇文档是用哪套参数切的」，重建索引也有据可依。

### 2.6 参数依据与风险

| doc_type | 现有数量 | 参数 | 依据 |
| --- | --- | --- | --- |
| spec | 15 | 400/60 | 规格表一行约 50 字，400 字约 8 行；块小让 rerank 更容易挑对，代价是块数增加约 25% |
| customs | 7 | 600/80 | 单证条款连续（如原产地规则），块太大会稀释，太小则一段规则被拆散 |
| compliance | 2 | 600/80 | 同上 |
| policy | 3 | 500/50 | 保持默认 |
| other | 1 | 500/50 | 保持默认 |

**风险**：知识库里同一文档的块大小不一致。检索侧不受影响 —— RRF 按排名融合、rerank 按 query-chunk 对打分，都对块长不敏感。但**评测基线会变**，必须重跑。

**不做的事**：不按扩展名分派。同一 `doc_type` 可能是 pdf 也可能是 docx，按业务类型分才稳定。

### 2.7 验证

| # | 操作 | 预期 |
| --- | --- | --- |
| 1 | 重新上传 `docs/samples/chunk-demo.md`（doc_type 选 spec） | 块长上限变为 400，`chunk_strategy` = `spec:400/60` |
| 2 | 造一个 5 段 × 200 字的 docx 上传 | 块数从 3 变 5（或 6），边界落在段落之间，不再腰斩 |
| 3 | 上传 21 行 xlsx | 每块首行都是完整表格行（以 `FL-` 或表头开头），不再出现半行 |
| 4 | 全文检索一篇 spec 文档 | 命中块能独立回答一个型号的完整参数 |
| 5 | 数据库核对 | `select doc_type, chunk_strategy, count(*) from doc_version join ... group by 1,2` 无 `default` 残留（存量重建后） |

### 2.8 回退

三处改动都是独立可逆的：`parser.py` 的 join 改回 `"\n"`，`line_aware` 传 `False`，`CHUNK_PROFILES` 全改 `(500, 50)`。回退后**必须重建索引**才会回到旧分片 —— 只回代码不重建，库里还是新分片。

---

## 3. 执行计划

| 批次 | 内容 | 验收 | 人日 |
| --- | --- | --- | --- |
| **第 1 批** | `query_understand.py` + `chat.py` 两处 + 配置开关（默认关）+ 指标 | 1.8 的三条对话 + 192 回归 | 1.5 |
| **第 2 批** | `parser.py` DOCX + `chunker.py` 行感知 + `pipeline.py` 分派 + `chunk_strategy` 落库 | 2.7 的五项 + 分片核对 | 1 |
| **第 3 批** | 存量 28 篇分批重建 + `retrieval_zh` 全量评测对比 | Recall 不低于基线 0.03 | 0.5 |
| **第 4 批**（观察后） | 术语归一词典 + 前端「已理解为」提示 | 抽 20 条真实问题看改写准确率 | 0.5 |

每批做完停下等你确认，出问题按批回退。

## 4. 需要你拍板的三件事

1. **`query_rewrite_enabled` 初始开还是关？** 我建议先关，跑一周 trace 看改写质量再开 —— 改写错了比不改更糟，且用户不易察觉。
2. **spec 的 400/60 认不认？** 这是唯一会明显改变存量分片的参数。如果你更看重「一块能答完一个型号」，可以给到 450/60；如果更看重命中精度，400 是合理的。
3. **改写结果要不要在问答界面显示给终端用户？** 显示更透明（避免「答非所问」的误解），但会让界面多一行字；也可以只在管理后台的链路详情里看。

确认后我从第 1 批开始，动手前照样先做文件快照。
