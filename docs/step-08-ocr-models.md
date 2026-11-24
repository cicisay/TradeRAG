# 步骤 8：OCR 扫描件识别 + 真实模型接入（嵌入 / 重排）

> 对应 PRD：FR-101（多格式解析）、FR-104（OCR 置信度人工抽检）、FR-205（多语言重排）、FR-206（引用可定位）、FR-702（可观测）
> 完成日期：2026-11-09

---

## 一、这一步解决了什么问题

| 之前的状态 | 现在 |
| --- | --- |
| 扫描件 PDF 解析为空 → 状态 failed | 逐页判断：文本层为空即渲染成图 → OCR 识别 → 正常入库 |
| 引用只有文档名，没有页码 | chunk 记录来源页码，引用显示“第 12 页” |
| 嵌入用哈希兜底（无语义能力，问“原产地证谁签发”会命中发票文档） | bge-m3 真实语义模型（1024 维，中/英/西/阿跨语言） |
| 检索只有 RRF 融合排序 | 融合后增加**重排**层（模型不可用时降级启发式，不会静默失效） |
| 不知道当前到底在用哪个模型 | `/api/v1/system/capabilities` 一眼看清实现与降级状态 |

---

## 二、交付内容

### 1. OCR 服务（`app/services/ocr.py`）

- 引擎：**RapidOCR（ONNXRuntime 版）**，纯 pip 安装、自带中英文模型、CPU 可跑，约 15MB；
  不选 PaddleOCR 是因为它需要 paddlepaddle 大依赖，Windows/Python 3.13 安装风险高。
- 可插拔：`BaseOcrEngine` 抽象出 `recognize(image_bytes) -> OcrResult`，要换 PaddleOCR / 云 OCR 只实现接口即可。
- 置信度：按**字符长度加权**平均每一行的识别得分（长行权重更大，比简单平均更贴近实际质量）。
- FR-104：低于 `OCR_MIN_CONFIDENCE`（默认 0.85）的页面记入 `doc_version.ocr_low_pages`，前端可提示“需人工抽检”。

### 2. 解析器改造（`app/services/parser.py`）

- **逐页判断**，不是整篇判断：
  - 文本层字符数 ≥ `OCR_MIN_TEXT_CHARS`（默认 20）→ 电子页，直接取文字（零成本）
  - 否则 → 扫描页，按 `OCR_DPI`（默认 200）渲染成 PNG 再 OCR
  - 这样“电子页 + 扫描页混排”的文档也能正确处理
- 页与页用换页符 `\f` 连接，作为页码定位的锚点。
- 单篇 OCR 页数上限 `OCR_MAX_PAGES`（默认 300），防止超大扫描件拖死请求。
- OCR 失败的页不会中断整篇：保留空文本并记录 `ocr_error`，文档仍可入库（可后续重跑）。

### 3. 页码定位（`parser.page_spans` + `chunker.split_text(page_spans=...)`）

- 切分器重构为**带偏移量**切分：每个段落记住自己在全文的起始位置 → 反查页码。
- 单页 PDF 没有换页符，用 `page_count == 1` 兜底识别（这是自检时发现的一个真实缺陷）。
- `chunk.page_no` 入库，引用快照 `page_or_section` 写“第 N 页”，Prompt 里也带上页码让模型能准确引用。

### 4. 真实嵌入（`app/services/embedding.py`）

| provider | 说明 |
| --- | --- |
| `auto`（默认） | 优先加载真实模型，失败自动降级 hash 并**告警** |
| `bge-m3` / `st` | 强制真实模型，加载失败直接报错（避免“以为在用模型其实在用兜底”） |
| `hash` | 仅开发/测试 |

- 模型引用解析 `resolve_model_ref()`：本地目录优先（`models/bge-m3`）→ 否则按模型名走网络。
- `HF_ENDPOINT=https://hf-mirror.com`（官方站被网络策略阻断），必须在校验前设置环境变量。
- 归一化输出（`normalize_embeddings=True`），使内积等价余弦，对齐 Milvus 的 IP 度量。
- **维度硬约束**：向量库新增维度校验——集合已是 1024 维时再写入 384 维会立即报错，
  而不是静默截断算出错误相似度（PRD 17.4 反锁定规则）。

### 5. 重排（`app/services/reranker.py`）

- `CrossEncoderReranker`：bge-reranker 交叉编码器，把“问题+候选”成对打分，sigmoid 压到 0~1。
- `HeuristicReranker`：无模型兜底——问题关键词覆盖率（0.7）+ 召回排名（0.3）。
  中文用二元组、英文用词切分，无需分词依赖。
- 检索链路：RRF 融合 → 取 `RERANK_TOP_N`（默认 20）候选 → 重排 → 截 `top_k`。
- 返回值新增 `rerank: {provider, applied, is_fallback, candidates}`，调试台可直接看到是否降级。

### 6. 可观测与运维工具

| 文件 | 作用 |
| --- | --- |
| `app/api/system.py` | `GET /api/v1/system/capabilities`：嵌入/重排/OCR/向量库/LLM 的实际状态；`?probe=true` 强制加载自检 |
| `app/services/warmup.py` | 启动后台线程预热模型，避免首个请求等几十秒 |
| `scripts/download_model.py` | 从 ModelScope **多线程分片**下载模型（hf-mirror 实测 0.4MB/s，太慢） |
| `scripts/reindex.py` | 重建索引：换模型或内存向量库重启后，把已解析文档重新嵌入入库 |

### 7. 数据库增量迁移

`doc_version` 新增 `ocr_used / ocr_confidence / ocr_low_pages / ocr_error`，`chunk` 新增 `page_no`。
`create_all` 只建表不加列，因此 `init_db()` 里加了幂等的 `ALTER TABLE ADD COLUMN` 补齐（SQLite 专用，生产走 Alembic）。

---

## 三、自检发现并修复的问题

| # | 问题 | 影响 | 处理 |
| --- | --- | --- | --- |
| 1 | 单页 PDF 无换页符 → 页码全为 null | 引用无法定位 | `page_spans(text, page_count)` 增加单页兜底 |
| 2 | 内存向量库不校验维度 | 换模型后 zip 静默截断，相似度算错但接口正常 | `ensure_collection` 记录维度，不一致直接报错 |
| 3 | 模型权重未下载时 sentence-transformers 会在请求内现场下载 | 上传请求超时（实测 120s 超时） | 预下载权重 + 启动后台预热 |
| 4 | 旧测试断言切分结果字段集合 | 新增 `page_no` 导致失败 | 改为断言必需字段子集 |
| 5 | 检索调试用例用了知识管理员账号 | 403（该角色无 `debug:retrieval` 权限） | 改用管理员账号，权限矩阵行为正确 |

---

## 四、验证结果

### 1. 自动化测试

```
pytest tests/            → 100 passed, 1 skipped
RUN_MODEL_TESTS=1 pytest tests/test_ocr_models.py → 11 passed
```

新增用例覆盖：扫描件 OCR（含置信度）、电子版 PDF 跳过 OCR、页码映射、跨页不合并、
启发式重排排序、检索结果带重排元数据、能力探针权限、真实模型维度与语义相似度。

### 2. 真实模型实测（CPU，bge-m3）

| 指标 | 结果 |
| --- | --- |
| 模型加载耗时 | 22.8 秒（所以必须启动预热，否则首个请求必超时） |
| 维度 | 1024 |
| 同义问法相似度（“不锈钢法兰的材质”vs“法兰用什么材料制造”） | **0.857** |
| 无关句相似度（vs“今天天气很好，适合出海钓鱼”） | 0.311 |
| **中英跨语言相似度**（“法兰的材质是304不锈钢”vs“Stainless steel flange, material grade 304”） | **0.647** |

跨语言 0.647 说明中文问题可以直接命中英文原文——这是哈希嵌入完全做不到的。

### 3. 重排实测（bge-reranker-base，CPU）

| 指标 | 结果 |
| --- | --- |
| 模型加载 | 14.0 秒 |
| 3 条候选重排耗时 | 0.29 秒（20 条约 1~2 秒，可接受） |
| 排序效果 | 问“不锈钢法兰用什么材质”：法兰 0.730 > 原产地证 0.500 = 商业发票 0.500 |

冒烟中重排器状态由 `heuristic` 自动切换为 `cross:models\bge-reranker-base`（`is_fallback=false`），
说明“模型就位即启用、缺失则降级”的策略按预期工作。

### 4. 真实服务冒烟（`data/smoke_s8.py`）

```
[OCR]   used=True conf=0.8789 low_pages=[] pages=2 chunks=2
[检索]  rerank={'provider': 'cross:models\bge-reranker-base', 'applied': True, 'is_fallback': False}
        - page=1 ... rerank_score=0.7308
        - page=2 ... rerank_score=0.5944      ← 页码正确
[跨语言] query="法兰用什么材质"
        - spec2.txt     不锈钢法兰 DN50 材质304 ...
        - spec_en.txt   Stainless steel flange, material grade 304 ...
[能力]  embedder: st:models\bge-m3, dim=1024, is_fallback=false
        reranker: cross:models\bge-reranker-base, is_fallback=false
        ocr: rapidocr, loaded=true
```

关键结论：2 页纯图片 PDF（8.3MB）→ OCR 置信度 0.879 → 切成 2 块且页码分别为 1、2；
中文提问同时命中中文与英文文档，语义检索 + 语义重排全链路打通。

---

## 五、已知边界

1. **OCR 精度**：RapidOCR 对印刷体中文/英文效果好，手写体、复杂表格、低分辨率扫描件会明显下降。
   低置信度页面已标记，但**没有做版面分析**（表格还原成 Markdown 结构属于后续增强）。
2. **重排模型需自行下载**：`scripts/download_model.py --model BAAI/bge-reranker-base`（1.1GB）。
   未下载时 `auto` 会用启发式重排（只反映词面覆盖，不理解同义改写），并在能力探针里标 `is_fallback=true`。
3. **OCR 同步执行**：目前在上传请求里同步跑，100 页扫描件会让请求等十几秒到几十秒。
   生产应改为 Celery 异步任务 + 前端轮询进度（M5 一起做）。
4. **chunk 级 OCR 置信度**：当前写入的是文档平均值，逐页精确值需要扩展 `chunk` 表存每页置信度。
5. **内存向量库重启即清空**：需执行 `scripts/reindex.py` 重建；接 Milvus 后此问题消失。
