# 文档上传 · 切分 · 评测 实操手册

> 适用版本：11-3（2026-11-17）。本文所有数字均为本机实测值，可直接对照验证。
> 配套样例：`docs/samples/chunk-demo.md`（上传它即可复现第 4 节的切分结果）

---

## 0. 一分钟速览

| 阶段 | 在哪做 | 输入 | 产出 | 你能看到的证据 |
|---|---|---|---|---|
| 上传 | 知识库页 `/app/knowledge.html` | PDF/DOCX/XLSX/TXT/MD | 文档记录 + 任务台账 | 列表出现文档，状态「排队」 |
| 解析 | 后台 worker | 原始文件 | 纯文本 + 页码映射 | 状态「已解析」，页数、OCR 置信度 |
| 切分 | 后台 worker | 纯文本 | 若干 chunk（约 500 字/块） | 点「分片」看每块内容 |
| 向量化 | 后台 worker | chunk 文本 | bge-m3 向量（1024 维） | 任务结果里 `chunks` / `indexed` 数 |
| 入库 | Milvus | 向量 + 元数据 | 可检索索引 | 问答能召回该文档 |
| 评测 | 管理后台「批量评测」 | 评测集 JSONL | Recall/MRR/nDCG 报告 | 历史跑分表 + JSON 报告文件 |

一句话：**上传只负责"收件"，解析和切分在后台队列里跑，切分结果可以直接在界面上逐块查看。**

---

## 1. 准备：确认服务与登录

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18080/health   # 期望 200
```

| 页面 | 地址 |
|---|---|
| 问答 | http://127.0.0.1:18080/app/ |
| 知识库（上传/分片） | http://127.0.0.1:18080/app/knowledge.html |
| 管理后台（评测） | http://127.0.0.1:18080/app/admin.html |

账号 `admin` / `admin123`，验证码必填（4 位，5 分钟有效、一次一用）。
上传需要 `kb:manage` 权限，`admin` 具备。

---

## 2. 模拟上传：两种方式

### 2.1 界面操作（推荐，能看到进度条）

1. 打开知识库页，在卡片区选一个知识库（如「外贸产品资料库」），点它选中（卡片变蓝边）。
2. 右下「上传文档」卡片里填四项：

| 字段 | 取值 | 含义 |
|---|---|---|
| 文档类型 | `spec` 产品规格 / `customs` 报关 / `compliance` 合规 / `policy` 政策 / `other` | 影响后续按类型筛选检索 |
| 权威级 | `T0`(0) 最高 → `T3`(3) 最低 | 冲突时高权威优先；T0 走合规双人复核 |
| 语种 | 留空自动识别，或填 `zh,en,es,ar` | 留空即可，解析后会按内容判定 |
| 选择文件 | 单文件，≤50MB | 白名单后缀 + 魔数校验 |

3. 点「上传」。**接口在毫秒级返回**，不会卡住等 OCR。
4. 观察三处变化：
   - 文档列表新行，状态「排队」→「解析中」→「已解析」
   - 右侧「任务队列」卡片进度条（解析 30% → 切分 32% → 向量化 32~95% → 完成 100%）
   - 完成后弹 toast：`《文件名》处理完成，N 个分片`

> 建议第一次就用 `docs/samples/chunk-demo.md`，第 4 节给出了它的精确预期结果。

### 2.2 命令行（便于脚本化、可复现）

```bash
# 1) 取 token（验证码从容器内生成，仅本机调试使用）
CAP=$(docker exec foreign-trade-rag-api-1 python -c \
  "from app.services.captcha import new_captcha;c,u,t=new_captcha();print(c+'|'+t)")
CID=${CAP%%|*}; TXT=${CAP##*|}
TOKEN=$(curl -s -X POST http://127.0.0.1:18080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"admin\",\"password\":\"admin123\",\"captcha_id\":\"$CID\",\"captcha_text\":\"$TXT\"}" \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 2) 看有哪些知识库，拿 kb_id
curl -s http://127.0.0.1:18080/api/v1/knowledge-bases -H "Authorization: Bearer $TOKEN"

# 3) 上传（表单字段全是 Form，不是 JSON）
curl -s -X POST http://127.0.0.1:18080/api/v1/documents/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "kb_id=1" -F "doc_type=other" -F "authority_level=2" -F "languages=" \
  -F "file=@docs/samples/chunk-demo.md"

# 4) 轮询任务（响应里带 job id 与 doc id）
curl -s "http://127.0.0.1:18080/api/v1/jobs/123" -H "Authorization: Bearer $TOKEN"
```

返回 `202` 表示已入队；若配置为 `INGEST_MODE=inline` 会同步跑完返回 `200`（仅测试/排障用）。

---

## 3. 上传之后，后台到底做了什么

任务台账表 `ingest_job` 记录五个阶段，前端进度条就是读它：

| stage | 中文 | 进度区间 | 说明 |
|---|---|---|---|
| `queued` | 排队 | 0 | 已登记，等 worker 领取 |
| `parsing` | 解析 | 3 → 30 | 读文件、抽文本；扫描件走 OCR，最慢 |
| `chunking` | 切分 | 32 | 按 500/50 规则切块 |
| `embedding` | 向量化 | 32 → 95 | 分批调 bge-m3，每批 8~64 条 |
| `indexed` | 完成 | 100 | 写 chunk 表 + Milvus |

几个和"能不能重复点"直接相关的机制：

- **幂等**：同一文档版本 + 同一流水线版本已有在途任务时，接口复用它而不是再排一个。连点五次不会索引五遍。
- **可重跑**：重新索引会先删旧 chunk 与旧向量再写新（`删旧写新`），所以重复执行不会产生重复召回。
- **失败重试**：网络/模型类错误按 3s、6s、12s… 退避重试，最多 `ingest_max_attempts` 次；解析为空、文件缺失这类业务错误标记为「不可重试」，直接失败，不浪费次数。
- **崩溃接管**：worker 被杀导致 stage 停在 `parsing`/`embedding` 且 `updated_at` 超过 `ingest_stale_seconds`，其他 worker 可以抢过来重跑。
- **取消**：协作式，只在批次边界生效，不强杀线程（避免留下半个库的脏数据）。

---

## 4. 文档是怎么切分的

### 4.1 算法四步（`backend/app/services/chunker.py`）

1. **按自然段切分**：以空行（`\n\s*\n`）为段落边界，同时记下每段在全文中的字符偏移。
2. **聚合成块**：把连续段落拼进同一个 buffer，直到再拼一段就超过 `max_chars=500` 为止。
3. **超长段硬切**：单段本身超过 500 字时，按 `每片 500 字、步进 450 字`（即重叠 50）机械切分。
4. **打标**：每块标注 `chunk_index`、`language`（zh/en/es/ar/mixed）、`page_no`（PDF 才有）。

两个容易被忽略的设计：

- **不跨页合并**：PDF 里如果一块横跨第 2、3 页，会在换页处强制断开。否则引用只能标"第 2 页"，用户翻过去找不到原文。
- **重叠 50 字**：防止答案恰好被切在边界。这是"多花 10% 存储换召回率"的取舍。

默认参数：

| 参数 | 值 | 位置 |
|---|---|---|
| `max_chars` | 500 | `chunker.DEFAULT_MAX_CHARS` |
| `overlap` | 50 | `chunker.DEFAULT_OVERLAP` |
| 语种判定阈值 | 中文字符占比 > 15% 判 `zh` | `detect_language()` |
| 嵌入模型 | bge-m3（1024 维） | `settings.embed_model` |
| 向量库 | Milvus（集合名见 `settings.milvus_collection`） | — |

### 4.2 用样例文件对照（docs/samples/chunk-demo.md）

样例是一篇 1628 字的中文文档，含 5 个自然段，其中第 4 段是 650 字无空行的超长段。实测结果：

| 块号 | 字数 | 语种 | 来自哪里 | 为什么是这样 |
|---|---|---|---|---|
| #0 | 440 | zh | 标题 + 商业发票段 + 装箱单段 | 440+1+160 > 500，第三段装不下 |
| #1 | 225 | zh | 原产地证段 | 单独成块 |
| #2 | 500 | zh | 超长段第 1 片 | 正好切到上限 |
| #3 | 200 | zh | 超长段第 2 片（含 50 字重叠） | 650−450=200 |
| #4 | 300 | zh | 中英混排段 | 中文占比仍超 15%，判 zh |

块合计 1665 > 原文 1628，多出来的部分是重叠与段落合并时的换行替换。

**重叠验证**（这就是你要观察的"重叠到底有没有生效"）：

```
chunk#2 末尾: …申报时限，出口货物应当在装货二十四小时以前向海关申报，转关运输与提前申报另有规定。第七项准备…
chunk#3 开头: 当在装货二十四小时以前向海关申报，转关运输与提前申报另有规定。第七项准备是关注退税政策…
```

`chunk#2[-50:] == chunk#3[:50]` 严格成立。

也可以在容器里自己跑一遍：

```bash
docker cp docs/samples/chunk-demo.md foreign-trade-rag-api-1:/tmp/d.md
docker exec -i foreign-trade-rag-api-1 python - <<'PY'
from app.services.parser import parse_file, page_spans
from app.services.chunker import split_text
r = parse_file('chunk-demo.md', open('/tmp/d.md','rb').read())
for p in split_text(r.text, page_spans=page_spans(r.text, r.page_count)):
    print(p['chunk_index'], len(p['content']), p['language'], p['content'][:40].replace('\n',' '))
PY
```

### 4.3 想改切分粒度怎么办

改 `chunker.py` 的两个常量即可，但要注意副作用：

| 调整 | 收益 | 代价 |
|---|---|---|
| `max_chars` 500 → 300 | 块更精准，引用更短 | 块数增约 60%，向量库与嵌入耗时同步上涨 |
| `max_chars` 500 → 800 | 上下文更完整，适合长条款 | 单块噪声变大，召回精度可能下降 |
| `overlap` 50 → 100 | 边界丢答案的概率更低 | 存储与去重成本上升，重复召回变多 |

改完必须**重建索引**（知识库页右上「重建索引」），否则旧文档还是旧切法。新旧切法混在一套索引里，Recall 会莫名其妙下降。

---

## 5. 怎么确认"它真的这么切了"

三个层次，按排查顺序：

### 5.1 界面（最直观）

知识库页 → 文档列表 → 该文档行右侧「分片」按钮 → 抽屉里逐块显示内容、语种、页码。这调的是：

```
GET /api/v1/doc-versions/{version_id}/chunks?offset=0&limit=100
```

### 5.2 接口（便于脚本核对字数）

```bash
curl -s "http://127.0.0.1:18080/api/v1/doc-versions/12/chunks?limit=100" \
  -H "Authorization: Bearer $TOKEN" \
  | python -c "import sys,json;d=json.load(sys.stdin);print('total',d['total']);[print(i['chunk_index'],len(i['content']),i['language'],i['page_no']) for i in d['items']]"
```

`version_id` 从文档列表或上一步上传响应里取。

### 5.3 数据库（确认元数据与状态）

```bash
docker exec foreign-trade-rag-postgres-1 psql -U rag -d rag -c \
  "select chunk_index, language, page_no, length(content) as chars from chunk \
   where doc_version_id=12 order by chunk_index;"
```

---

## 6. 怎么评测

### 6.1 先分清两种评测

| 类型 | 评什么 | 现在有吗 | 指标 |
|---|---|---|---|
| 检索质量 | 该找到的文档/条款，有没有进 Top-K | 有，内置 3 个集 | Recall@K、MRR、nDCG |
| 合规判定 | 该拦的违规问题有没有拦住 | 有，compliance 集 | 准确率、红线漏放、误拦 |
| 答案质量 | 回答是否忠于原文、有没有编造 | **暂未接入**（P2 待做） | faithfulness / answer_relevance |

也就是说：**现在能自动化评测"检索准不准 + 合规判得对不对"，还不能自动化评测"答案写得好不好"。** 后者目前靠人工看链路详情与 badcase 沉淀。

### 6.2 内置评测集

| 数据集 | 条数 | 类型 | 用途 |
|---|---|---|---|
| `retrieval_zh` | 55 | 检索 | 中文单语检索基线 |
| `retrieval_crosslingual` | 25 | 检索 | 跨语言（中→英/西/阿） |
| `compliance` | 36 | 合规 | 违规拦截与放行 |

文件就在 `eval/*.jsonl`，一行一题。**注意：这个目录必须挂载进容器**（`docker-compose.yml` 里 `./eval:/eval:ro`），否则批量评测的下拉框是空的 —— 这是已经踩过的坑，别删那一行。

格式：

```json
{"id":"R001","query":"不锈钢法兰用什么材质","expected_docs":["法兰规格书_zh.txt"],"expected_keywords":["304 / 316L"],"lang":"zh","category":"product"}
{"id":"P001","query":"DAP和DDP有什么区别","expected_action":"pass","lang":"zh","category":"normal"}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 唯一编号，报告里用它定位 |
| `query` | 是 | 用户会怎么问，尽量贴近真实口语 |
| `expected_docs` | 检索集必填 | 期望命中的文档名（支持子串匹配，不要求全等） |
| `expected_keywords` | 可选 | 期望出现在块内容里的关键词，补充判定 |
| `expected_action` | 合规集必填 | `pass` / `annotate` / `block` |
| `lang` | 是 | `zh`/`en`/`es`/`ar`，用于分组看掉分 |
| `category` | 是 | 分组维度，如 `product`/`customs`/`crosslingual` |

### 6.3 判分口径（重要，别按教科书理解）

- **命中判定**：先看 `doc_name` 是否包含 `expected_docs` 中任一项（双向子串匹配），不中再看块内容里有没有 `expected_keywords`。命中即算相关。
- **Recall@K**：报告里的 `recall` 实际是 **hit-rate@K** —— 每题命中记 1、未命中记 0，再取平均。它不区分"5 个相关文档只召回了 1 个"这种情况，所以数值会**高于**教科书定义的 recall。看趋势够用，别拿去和论文数字比。
- **MRR**：`1 / 首个命中的排名`，没命中记 0。衡量"第一个正确结果排得多靠前"。
- **nDCG@K**：按位置折损（`1/log2(i+2)`）累加命中，再除以理想 DCG。衡量"排序整体好不好"。
- **合规准确率**：`判定动作 == 期望动作` 的题目占比，另外单独统计「红线漏放」（该拦没拦）与「误拦」（不该拦却拦了）。

### 6.4 跑一次

管理后台 → 左侧「批量评测」：

| 控件 | 建议值 | 说明 |
|---|---|---|
| 评测集 | `retrieval_zh` | 第一次跑中文集最快（55 题，约 1 分钟） |
| Top K | 5 | 与线上问答默认一致 |
| 限制条数 | 留空=全部 | 调试时填 5~10，几十秒出结果 |
| 重排 | 勾选 | 不勾等于只测召回层，数值会明显偏低 |

点「开始跑分」后：按钮变「跑分中…」，逐题检索（单题约 0.3~1.5s，跨语言集更慢），完成后展示三通道对比、按语种/类别分组明细、未命中用例清单，并写入「历史跑分」。

命令行等价：

```bash
curl -s -X POST http://127.0.0.1:18080/api/v1/debug/eval/run \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"dataset":"retrieval_zh","top_k":5,"limit":10,"rerank":true,"include_details":true}'
```

### 6.5 报告怎么读

一次跑分产出三份东西：

1. **界面结果**：三通道（vector / bm25 / hybrid）各自的 Recall、MRR、nDCG；`by_lang`、`by_category` 分组。
2. **数据库**：`eval_run` 表一行，含汇总指标与完整明细 JSON。
3. **落盘文件**：`backend/data/eval_reports/retrieval_zh-20261117-101530.json`。

读的时候按这个顺序看：

| 现象 | 大概率原因 | 下一步 |
|---|---|---|
| hybrid 明显高于 vector 与 bm25 | 正常，融合生效 | 记录基线 |
| bm25 高于 vector | 嵌入模型不适配该语种，或文档未正确入库 | 查 `indexed` 数、换模型对比 |
| 某语种分组接近 0 | 该语种文档没入库，或 `lang` 标错 | 看文档语种字段与 chunk 语种分布 |
| Recall 高但 MRR 低 | 召回到了，但排序差 | 调 rerank（`rerank_top_n`） |
| 全部通道都低 | 期望文档名写错（子串匹配对不上） | 打开明细看 `top_docs` 实际是什么 |
| 准确率 100% 但耗时 0ms | 合规集短路了，检查是否只跑了 5 条 | 看报告 `cases` 字段 |

### 6.6 加一道新题

追加一行到对应 `eval/*.jsonl` 即可，无需重启（每次跑分实时读文件）：

```bash
cat >> eval/retrieval_zh.jsonl <<'EOF'
{"id":"R056","query":"出口沙特需要什么认证","expected_docs":["沙特认证说明_zh.txt"],"expected_keywords":["SABER","SCoC"],"lang":"zh","category":"customs"}
EOF
```

两个坑：`expected_docs` 必须是**真实存在的文档名**（去知识库列表里复制）；JSONL 一行一条，别写成数组。

---

## 7. 常见问题

| 症状 | 检查点 |
|---|---|
| 上传报 422「不支持的文件类型」 | 只收 `.txt .md .pdf .docx .xlsx`；`.doc/.xls` 老格式要另存 |
| 上传报 413 | 超过 `max_upload_mb`（默认 50MB） |
| 状态一直「排队」 | worker 没起：`docker ps \| grep worker`；或 Redis 不通 |
| 状态「已解析」但分片为 0 | 文档只有空白/图片且 OCR 未启用，解析结果为空 |
| 分片数远多于预期 | 文档里空行多（每段都独立），或表格被抽成大量短行 |
| 问答召回不到新文档 | 文档 `status` 不是 `active`、业务组不匹配、或索引未完成 |
| 批量评测下拉框为空 | `eval/` 未挂载进容器（`./eval:/eval:ro`），或目录下没有 `*.jsonl` |
| 评测跑完 Recall 全是 0 | 期望文档名与库里 `doc_name` 对不上（子串也不匹配） |

---

## 8. 速查表

```
上传     POST /api/v1/documents/upload          Form: kb_id, file, doc_type, authority_level, languages
知识库   GET  /api/v1/knowledge-bases           拿 kb_id
任务     GET  /api/v1/jobs/{id}                 看 stage / progress / result.chunks
分片     GET  /api/v1/doc-versions/{vid}/chunks?limit=100
重建索引 POST /api/v1/system/reindex            改切分参数后必须执行（默认后台逐篇入队）
评测集   GET  /api/v1/debug/eval/datasets
跑分     POST /api/v1/debug/eval/run            JSON: dataset, top_k, limit, rerank
历史     GET  /api/v1/debug/eval/runs?limit=10
```

| 关键数字 | 值 |
|---|---|
| 切分上限 / 重叠 | 500 字 / 50 字 |
| 嵌入维度 | 1024（bge-m3） |
| 上传上限 | 50MB |
| 文档类型 | spec / customs / compliance / policy / other |
| 权威级 | 0=T0（最高）→ 3=T3 |
| 评测集 | retrieval_zh 55 / retrieval_crosslingual 25 / compliance 36 |
