# 步骤 5 实施记录：混合检索 + 跨语言双路 + RBAC 检索调试台（M3 检索层）

日期：2026-11-09　对应里程碑：M3　对应 PRD：FR-202/203/603/700/704/707

## 本步做了什么

### 1. BM25 关键词检索（app/services/bm25.py）
- 中文按**汉字二元组**切分（免 jieba 依赖），英文/数字按单词，小写归一
- 标准 BM25（k1=1.5, b=0.75），IDF + 长度归一
- 索引每次从 DB 可见 chunk 构建（≤5000 条），生产换 Milvus 稀疏/OpenSearch

### 2. 翻译服务（app/services/translator.py）
- Qwen（OpenAI 兼容协议）翻译查询为英/西/阿变体，10s 超时
- 未配 Key / 调用失败 → 返回空变体 + degraded=True（FR-202 降级原则，不报错）

### 3. 混合检索（app/services/retrieval.py）
- 向量路：bge-m3/兜底嵌入 + Milvus/内存库，**expr 强制 build_access_expr 过滤**
- BM25 路：SQL 层过滤业务组/文档状态/知识库（与管理员的通配逻辑分离）
- **RRF 融合**（k=60）：按排名而非原始分；同 chunk 跨路命中合并 channels
- 跨语言路：变体翻译成功则各语种并行向量+BM25 检索后再融合
- 召回结果统一回查 chunk/document/meta 补全内容与出处；已删除向量防御性丢弃

### 4. 检索调试台 API（app/api/debug.py）
- POST /api/v1/debug/retrieval：五通道对照（vector/bm25/hybrid + variants）、耗时、degraded
- 权限 debug:retrieval（FR-707 仅管理员/审计员）；**simulate_user 仅管理员可用**（FR-704 RBAC 模拟）

### 5. 测试（40/40 全过，新增 10 例）
- 分词/BM25 精确词排序/空索引；RRF 合并且跨路分数叠加
- 接口：普通用户 403；管理员五通道有结果且首条相关；模拟 biz_a 用户全通道 0 越权；模拟权限仅管理员；无 Key 时 degraded+空 variants
- 业务组隔离真实数据验证：biz_b 用户检索 → vector/bm25/hybrid 均 0 命中

## 自查发现的问题

- 管理员向量路 expr 中 `biz_group in ["*"]` 无法在内存实现里通配：改为管理员走 expr=None + kb 范围二次过滤（记录在代码注释）。生产 Milvus 可用 `biz_group != "_none_"` 或省略该条件，届时按注释调整。
- 密级：用户表暂无 sensitivity 字段（PRD user 表未定义），当前普通用户上限按 0 处理，管理员不受限。密级体系在合规步骤（M4）统一建模。

## 验证结果

- pytest：**40 passed**
- 冒烟：上传法兰规格文档 → admin 检索 hybrid 1 条（channels=[bm25,vector]）→ 模拟 biz_b 用户 vector/bm25/hybrid 全 0 → 隔离生效 ✅

## 已知待办

- 生成层：LLM 回答 + SSE 流式 + 引用原文快照（下一步 M3 核心）
- 翻译 Key 配置后的跨语言效果验证（需用户提供 QWEN_API_KEY）
- 查询改写/术语映射（FR-207）、HyDE（FR-208）留待生成步骤一并做
- SQL 通道（M4 五铁律）
