# 步骤 4 实施记录：切分 + 嵌入 + 向量入库（M2 第二部分）

日期：2026-11-09　对应里程碑：M2　对应 PRD：6.1 chunk / 6.2 Milvus Schema / 6.3 索引 / 17.4 反锁定 / FR-603

## 环境结论（重要）

- 本机**无 Docker** → Milvus 无法启动；已按约定采用**同接口内存向量库兜底**，Milvus 实现代码已按 PRD 6.2 写完（HNSW M=16/efConstruction=200、度量 IP、标量字段齐全），装上 Docker 后改 `VECTOR_STORE=milvus` 即切换。
- 本机无 bge-m3 模型（约 2.3GB）→ 嵌入器可插拔：`EMBED_PROVIDER=bge-m3`（生产）/ `hash`（开发测试，确定性 1024 维字符 3-gram）。加载失败自动降级并告警。

## 本步做了什么

### 1. chunk 表（app/models/chunk.py）
doc_version_id / chunk_index / content / language / parent_chunk_id / ocr_confidence / status，对齐 PRD 6.1。

### 2. 切分器（app/services/chunker.py）
- 自然段聚合到 ≤500 字（可配），超长段硬切，overlap 50 防边界丢答案
- 语种探测：Unicode 区间启发式 → zh/en/es/ar/mixed（无需模型）
- 文案：1774 字报关指南 → 4 块，每块 ≤500 字 ✅

### 3. 嵌入器（app/services/embedding.py）
- `BgeM3Embedder`（FlagEmbedding 延迟导入，1024 维，多语言）
- `HashEmbedder`（开发兜底：字符 3-gram → SHA-256 桶 → TF → L2 归一化；同文本同向量，可复现）
- 单例 + 自动降级，上层只认 `embed()` 接口（反锁定规则 1）

### 4. 向量库适配层（app/services/vector_store.py）
- `VectorRecord`：字段严格对齐 PRD 6.2（含 chunk_type/language/doc_status/authority_level/content_hash），缺关键字段直接报错
- `build_access_expr()`：**唯一**的访问过滤表达式构造入口，强制拼入 kb_id/biz_group/sensitivity/doc_status —— FR-603 的落地闸门，禁止业务代码手写
- `InMemoryVectorStore`：IP 余量检索 + 简化 expr 求值 + 按版本删除；`MilvusVectorStore`：pymilvus 完整实现（延迟导入，未装依赖不影响启动）
- `get_vector_store()` 单例 + 降级，`reset_*` 供测试

### 5. 索引流水线（app/services/pipeline.py）
解析文本 → 切分 → 批量嵌入 → 写 chunk 表 → 写向量库（标量字段齐全）→ 回填 pipeline_trace_id。
幂等：重跑先删同版本旧 chunk/向量（删旧写新，PRD 6.3）。已接入上传接口（同步；Celery 异步化在后续步骤）。

### 6. 测试（30/30 全过）
- 切分 4 例（空/段落/硬切/四语种探测）
- 嵌入 3 例（维度、归一化、确定性、相似度排序）
- 向量库 4 例（业务组过滤、密级+状态过滤、按版本删除、缺字段报错）
- 流水线集成 2 例（上传端到端 chunk 数一致 + trace_id；重跑幂等）

## 自查发现并修复的 Bug（2 个疏漏）

1. `vector_store.py` 使用 logger 但未 import/定义 → NameError，Milvus/内存实现均无法实例化。
2. 测试文件清理动态导入时误删 `import pytest` → 1 个用例 NameError。

## 验证结果

- pytest：**30 passed**
- 真实服务冒烟：上传 1774 字报关指南 → `chunks=4`、`status=parsed`、DB chunk 4 行、trace_id 回填、语种 zh ✅

## 已知待办（M2 剩余）

- 扫描件 OCR（PaddleOCR 中/英/西/阿）+ OCR 质量页
- 翻译副本 chunk_trans + 多语言变体检索（FR-202）
- BM25 倒排索引（混合检索的另一路）
- Celery 异步流水线 + 失败重试/逐阶段状态（构建流水线页数据）
- Milvus 实机联调（需 Docker）
