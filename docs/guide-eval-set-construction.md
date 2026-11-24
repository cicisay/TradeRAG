# RAG 评测集构建规范

> 面向本项目的可执行规范。先诊断现有 116 条能做什么、不能做什么，再给分层设计与造题方法。

## 0. 一句话结论

现有评测集**能看趋势，不能下判断**。指标虚高的根因不是题少，是判据太松；而答案层评不了的根因不是没接 RAGAS，是数据里根本没有标准答案字段。

---

## 1. 先诊断：现有 116 条的真实水位

### 1.1 构成

| 集合 | 条数 | 字段 | 现状 |
| --- | --- | --- | --- |
| `retrieval_zh.jsonl` | 55 | id/query/expected_docs/expected_keywords/lang/category | 中文检索 |
| `retrieval_crosslingual.jsonl` | 25 | 同上 | 中文问、查外文文档 |
| `compliance.jsonl` | 36 | id/query/expected_action/lang/category | 三层合规判定 |

类别分布：product 20 / customs 10 / compliance 9 / policy 6 / incoterms 5 / crossdoc 5。
合规集：pass 8 / annotate 21 / block 7，覆盖 sanction_country、dual_use、entity_list、gdpr、evasion 等 7 类。**这套是三套里最扎实的**，判据明确（`expected_action` 三分类），可以直接用于回归。

### 1.2 三个"松"

**松一：判据到文档，不到块。**

`evaluation.py:112` 的 `_is_relevant`：

```python
def _is_relevant(hit: dict, case: EvalCase) -> bool:
    doc = (hit.get("doc_name") or "").lower()
    for expected in case.expected_docs:
        e = expected.lower()
        if e and (e in doc or doc in e):
            return True                      # ← 文档名对上就算相关
    content = (hit.get("content") or "").lower()
    return any(k.lower() in content for k in case.expected_keywords if k)
```

一篇规格书切成十几二十块，**命中封面、命中目录、命中免责声明都算成功**。所以 `hit=1` 的信息量很低。

**松二：query 的用词和文档高度重合。**

实测 55 条里 **21 条（38%）的 query 直接包含文档核心名**：

| query | 期望文档 | 问题 |
| --- | --- | --- |
| 不锈钢法兰用什么材质 | 法兰规格书_zh.txt | "法兰"二字直接给出文档 |
| 法兰的公称压力是多少 | 法兰规格书_zh.txt | 同上 |
| 法兰执行什么标准 | 法兰规格书_zh.txt | 同上 |

BM25 靠字面就能命中，向量检索的价值体现不出来。真实用户会说「304 和 316L 哪个耐氯离子」「我那批货被扣了说材质不对」——现有集里一条都没有这种问法。

**松三：单点判据。**

平均 1.1 个关键词、1.1 个期望文档。只要那个词没出现在 chunk 里（哪怕是同义改写），就判 0；只要出现，就判 1。二值判断，没有中间态。

另外发现一处隐患：2 条的关键词写成 `"304 / 316L"`（含空格斜杠）。原文若是分行写的，这个整体子串永远匹配不上 —— 这类"看起来对其实永远判错"的样本，比没有样本更危险。

### 1.3 所以能做什么

| 用途 | 可以吗 | 说明 |
| --- | --- | --- |
| 改前/改后趋势对比 | ✅ | 同一批题、同一套判据，相对变化可信 |
| 判断"检索是否变差" | ✅ | 回归守门够用 |
| 判断"检索好不好" | ❌ | 没有绝对基准，Recall 1.0 不代表没问题 |
| 评测答案质量 | ❌ | 无 answer 字段 |
| 评测"该拒答时有没有拒答" | ❌ | 无负样本 |
| 横向对比其他方案 | ❌ | 判据太松，区分不出高低 |

---

## 2. 目标结构：四层评测集

不同层评的是不同环节，判据、规模、维护频率都不同，**不要塞进一个文件**。

| 层 | 评什么 | ground truth | 核心判据 | 目标规模 | 更新节奏 |
| --- | --- | --- | --- | --- | --- |
| **L1 检索层** | 找得准不准 | `gold_chunk_hashes` | Recall@K（块级）/ nDCG@K / MRR | 300 | 每月 |
| **L2 答案层** | 答得对不对 | `answer_points` + `reference_answer` | 要点覆盖率 + faithfulness（RAGAS） | 150 | 每季 |
| **L3 合规层** | 该不该拒 | `expected_action` | 三分类混淆矩阵 + block_recall | 60（现有 36） | 随词库 |
| **L4 端到端** | 多轮/降级/边界 | 对话脚本 + 断言 | 人工验收清单 | 30 | 每季 |

四层的关系：L1 差则 L2 必然差（检索不到就答不对），但 L1 好不代表 L2 好（检索对了模型可能答偏）。所以要分开测，出问题时才知道是检索的锅还是生成的锅。

---

## 3. 字段规范（关键设计）

### 3.1 不能存 chunk_id

`chunk.id` 是数据库自增主键。重建索引会先删后插（`pipeline.py:74-82`），**同一段文字重建后 id 全变**。把 id 写进评测集，下次重建后所有标注全部失效，而且不会报错——只是指标悄悄掉下来，你会以为是检索退化了。

### 3.2 用内容指纹

```python
import hashlib, re

def chunk_fingerprint(text: str) -> str:
    """块内容指纹。切分参数不变、文档不改，重建索引后指纹不变。"""
    norm = re.sub(r"\s+", "", text or "")     # 去所有空白，抵消排版差异
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
```

性质：
- 文档内容不变 + 切分参数不变 → 指纹不变，标注长期有效
- 切分参数改变 → 块边界变、内容变、指纹变 → **必须重新标注**（这本身就是应该发生的）
- 文档改版 → 指纹变 → 标注失效，需复核

代价是每次改切分参数都要重标一遍。这个代价是合理的：判据严密的评测集本来就该跟着索引走。

### 3.3 完整字段

```json
{
  "id": "R101",
  "query": "我们那批不锈钢法兰被海关扣了，说是材质不符合标准，可能是什么原因",
  "lang": "zh",
  "category": "customs",
  "difficulty": "hard",
  "source": "expert",
  "gold_chunk_hashes": ["a1b2c3d4e5f60718", "9f8e7d6c5b4a3021"],
  "expected_docs": ["法兰规格书_zh.txt"],
  "expected_keywords": ["304", "316L"],
  "answer_points": [
    "材质需符合 HG/T 20592-2009",
    "304 适用于常规介质，316L 耐氯离子",
    "需提供材质证明书"
  ],
  "reference_answer": "……（完整参考答案，L2 判分基准）",
  "should_refuse": false,
  "notes": "跨段推理：需同时命中材质表与标准条款"
}
```

字段说明：

| 字段 | 必填 | 用途 |
| --- | --- | --- |
| `gold_chunk_hashes` | L1/L2 必填 | 唯一严密判据 |
| `answer_points` | L2 必填 | LLM-as-judge 按要点打勾，比整段比对稳定 |
| `reference_answer` | L2 选填 | 有则用，无则只按 answer_points 判 |
| `should_refuse` | 负样本必填 | true 表示"正确行为是明确说未覆盖"，命中任何 chunk 都不算成功 |
| `difficulty` | 必填 | easy/medium/hard，用于分层看指标（只看总分会被简单题稀释） |
| `source` | 必填 | log/synthetic/expert/badcase，用于判断数据集的可信度 |
| `expected_docs` | 保留 | 兼容旧判分与看板 |
| `expected_keywords` | 降级 | 仅作辅助提示，不再作为主判据 |

### 3.4 判分改造（`evaluation.py`）

```python
def _is_relevant(hit: dict, case: EvalCase) -> bool:
    # 负样本：任何命中都不算"检索成功"，走 should_refuse 单独判
    if case.should_refuse:
        return False
    # 主判据：内容指纹
    if case.gold_chunk_hashes:
        if chunk_fingerprint(hit.get("content") or "") in case.gold_chunk_hashes:
            return True
        if case.expected_docs:               # 指纹没中，退回文档级但打标
            doc = (hit.get("doc_name") or "").lower()
            return any((e.lower() in doc) for e in case.expected_docs if e)
        return False
    # 老集：维持原逻辑
    ...
```

同时新增两个指标，别动 `recall` 这个 key（Grafana 看板在用）：

| 指标 | 算法 | 为什么需要 |
| --- | --- | --- |
| `recall`（保留原名） | 现有 `hit`，即 hit-rate@K | 兼容看板；文档里必须注明"这是命中率不是召回率" |
| `chunk_recall`（新增） | `len(gold ∩ retrieved) / len(gold)` | **这才是真召回率**，能看出"两个金标块只找到一个" |
| `precision_at_k`（新增） | `len(gold ∩ retrieved) / k` | 看 top-k 里有多少是噪音 |

`chunk_recall` 是本方案最有价值的产出：现有指标里，一个问题的两个金标块命中一个就算满分，实际问题答案是不完整的。

---

## 4. 数据从哪来

按价值排序，四个来源的性价比差别很大：

| 来源 | 方法 | 优点 | 缺点 | 单条成本 | 建议占比 |
| --- | --- | --- | --- | --- | --- |
| **真实日志** | 从 `message` 表拉全量 query → 语义聚类去重 → 挑高频与长尾 → 人工标 gold | 最贴近真实分布，能发现你没想到的问法 | 系统刚上线，量不够；需脱敏 | 5 min | 30% |
| **专家出题** | 找 1–2 名外贸业务员按真实场景写 | 唯一能测出真实水平的来源 | 人力成本高，需给模板 | 15 min | 20% |
| **文档反推** | 每个 chunk 让 LLM 生成 2–3 个问法，人工筛 | 快，gold chunk 天然已知 | **易与原文用词重合，指标虚高** | 2 min | 40% |
| **badcase 回流** | 点踩 → `append_badcase_sample` 写草稿集 | 自动积累，直击痛点 | 需人工补 gold 才能入库 | 5 min | 10% |

### 4.1 文档反推的正确姿势（最容易做错）

现有 38% 的字面重合就是"照原文术语出题"造成的。**必须强制改写**，让 LLM 生成三种变体：

| 变体 | 示例（原文：`材质：304 / 316L，适用于水、蒸汽`） |
| --- | --- |
| 口语化 | 「那个不锈钢的法兰是用什么料做的」 |
| 换术语 | 「法兰的钢材牌号有哪几种」 |
| 加场景 | 「化工管道上用哪种法兰不容易生锈」 |

Prompt 关键约束：

```
根据下面这段文档生成 3 个用户可能提的问题。要求：
1. 不要照抄原文术语，用同义说法或口语表达
2. 至少 1 个带业务场景（如报关、验货、退运、客户投诉）
3. 不要出现文档标题里的词
4. 问题要能仅凭这段内容回答
```

第 3 条是硬约束——正是它挡掉了那 38%。

### 4.2 负样本怎么造（现在完全缺失）

没有负样本，就测不出 FR-213「知识库未覆盖时不许编」。四类：

| 类型 | 例子 | 正确行为 |
| --- | --- | --- |
| 领域外 | 「今天美元兑人民币中间价多少」 | 走 SQL 模板；无数据则明确说无 |
| 时效超出 | 「2026 年 12 月生效的新规是什么」 | 说明知识库截止时间 |
| 权限外 | 用 demo_kb 账号问别的知识库内容 | 命中 0 条，说明可见范围 |
| 伪前提 | 「法兰的 PN16 是 16 公斤吗」（前提错误） | 纠正而非顺着答 |

判据：`should_refuse=true` 时，**检索到任何 chunk 都不算对**，要看最终回答是否包含"未覆盖/无法确认"类表述，且不含编造的具体数字。

---

## 5. 规模与配比

### 5.1 规模节奏

| 阶段 | 总量 | 触发条件 |
| --- | --- | --- |
| 现在 | 116 | 基线 |
| 一期 | 300 | 能看出分层差异 |
| 二期 | 540 | 上线交付 |

不要一次做 1000 条。**评测集的边际收益递减很快**：前 100 条能发现大问题，100→300 能看出分层，300 以后主要是防回归。

### 5.2 配比（按真实流量，不是按文档数量）

| category | 占比 | 条数（一期 300） | 理由 |
| --- | --- | --- | --- |
| product | 35% | 105 | 规格书最多（15 篇），查询也最频繁 |
| customs | 20% | 60 | 单证问题直接关联钱 |
| compliance | 15% | 45 | 高风险，错一次代价大 |
| policy | 12% | 36 | 内部制度 |
| incoterms | 10% | 30 | 术语换算类 |
| crossdoc | 8% | 24 | 跨文档推理，最能暴露融合策略好坏 |

难度：easy 30% / medium 50% / hard 20%。

`crossdoc` 刻意保留 8%：单文档题靠 BM25 就能过，**只有跨文档题才能测出 RRF 融合和 rerank 的真实价值**。

---

## 6. 质量校验：怎么知道这套题是有效的

三条硬检验，任何一条不过就说明题目有问题：

### 6.1 区分度检验（最重要）

同一批题，跑两种配置，指标必须拉开差距：

| 对比 | 期望 |
| --- | --- |
| `rerank=True` vs `False` | nDCG 至少差 0.05 |
| `hybrid` vs 纯 `vector` | Recall 至少差 0.05 |
| `crosslingual=True` vs `False`（跨语言集） | Recall 至少差 0.15 |

**如果三种配置得分几乎一样，这套题就是废的**——它测不出任何方案差异，只能测出"系统还活着"。

### 6.2 盲测天花板

拿全部 gold 题跑当前系统：Recall@5 若 ≥ 95%，说明题太简单，要加难度；若 ≤ 60%，说明 gold 标注可能有错（或系统真有大问题，先查 gold）。

合理区间：**75%–90%**。留出改进空间，也不至于全是错题。

### 6.3 标注一致性

抽 20% 双人独立标注 `gold_chunk_hashes`，Cohen's kappa ≥ 0.7。低于 0.7 说明"什么算相关"的定义本身有歧义，要先改标注规范再继续。

---

## 7. 落地步骤

| 批次 | 内容 | 产出 | 人日 |
| --- | --- | --- | --- |
| **1** | 改 `EvalCase` 加字段 + 改 `_is_relevant` + 加 `chunk_recall`；给现有 55 条补 `gold_chunk_hashes`（脚本：跑一次检索取 top1，人工确认/修正） | 判据变严，指标会**下降**（这是好事） | 1 |
| **2** | 文档反推补 150 条（强制改写 + 场景化），覆盖 6 类 | 一期 300 条 | 1 |
| **3** | 负样本 40 条 + 专家出题 50 条 | 补齐 L4 | 1 |
| **4** | 接通 RAGAS：有了 `answer_points` 才能跑 faithfulness / answer_relevancy | L2 面板不再空壳 | 1.5 |

第 1 批完成后指标一定会掉（可能从 1.0 掉到 0.7 左右）。**这不是退化，是原来虚高**。要在文档和看板上提前说明，否则会被误判为改坏了。

## 8. 与 RAGAS 的衔接

RAGAS 的四项核心指标对数据的要求：

| 指标 | 需要什么 | 本项目现状 |
| --- | --- | --- |
| faithfulness | answer + contexts | 有 answer（`message.content`）有 contexts（`ref_snapshot`），**可跑** |
| answer_relevancy | question + answer | 可跑 |
| context_precision | question + contexts + ground_truth | 缺 ground_truth，需 `gold_chunk_hashes` |
| context_recall | 同上 | 同上 |

所以顺序上是：**先有 gold 标注，RAGAS 的 precision/recall 才有意义**。先装 RAGAS 只能跑前两项，这正是 11-2 里 `faithfulness` 面板留空的原因——不是没装，是 `RefSnapshot` 里存了 contexts 但评测集没有 ground truth 配对。

## 9. 四个常见陷阱

1. **只加同类型的题**。现有 116 条已经饱和（Recall 1.0），再加 100 条同款只会让指标更漂亮、更没用。要加"现在答不对的题"。
2. **用 chunk_id 做标注**。重建索引后静默失效，指标掉了还以为检索退化。
3. **让 LLM 照原文出题**。这就是 38% 字面重合的来源，等于给自己送分。
4. **只看总分**。必须按 `difficulty` 和 `category` 分层看 —— 简单题占 70% 时，总分再高也说明不了什么。

## 10. 一句话建议

先做第 1 批（判据改严 + 补 55 条金标）。它花 1 天，但会让你第一次看到**真实的**检索水平——大概率比现在低不少，那才是起点。
