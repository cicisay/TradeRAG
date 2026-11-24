# 步骤 11-1：Langfuse 全链路追踪（本地 Docker 部署）

> 目标：对 RAG 的完整链路（问题 → 合规预检 → 翻译变体 → 向量/BM25 检索 → 融合重排
> → LLM 生成 → 输出合规 → 落库）做可视化追踪，回答"一次问答到底慢在哪、哪一步空了、
> 大模型用了多少 token"。

## 1. 为什么选 Langfuse v2 而不是 v3

| 项 | v2（本方案） | v3（官方最新） |
|---|---|---|
| 容器数 | **2**（server + 自带 PG） | 6（web/worker/clickhouse/minio/postgres/redis） |
| 额外内存 | **约 0.6 GB** | 约 2.2 GB |
| 追踪能力 | trace/span/generation、token、延迟、评测——本项目全部够用 | 额外多 agent/实验功能 |
| 适合 | 单机自用、个人项目 | 团队/生产规模 |

机器上已跑着 RAG 全栈（约 1.7 GB），v3 的 ClickHouse 栈会把 Docker 内存顶到临界。
v2 是官方维护的 LTS 线，能力覆盖本项目全部追踪需求；将来要升级 v3 只需换镜像和 compose。

## 2. 架构

```
浏览器 ──► nginx:18080 ──► api ──┬──► postgres / milvus / redis（原有）
                                 │
                                 └──► langfuse:3000（新增，profile=obs）
                                        └──► langfuse-db（独立 PG，数据卷隔离）
管理界面：http://127.0.0.1:13000
```

- Langfuse 两个服务带 `profiles: ["obs"]`，**默认 `docker compose up` 不启动**，
  不占任何内存；要看追踪时 `docker compose --profile obs up -d`。
- 实际镜像版本：`langfuse/langfuse:2` → **2.95.11**（v2 LTS 线）。
- API Key 通过 `LANGFUSE_INIT_*` 无头初始化预置（组织/项目/账号全自动创建），
  首次启动即可用，无需手动注册。
- key 同时写进 `backend/.env`（api/worker 通过 env_file 注入），两边一致。

## 3. 后端埋点设计（`app/services/tracing.py`）

设计原则：

1. **零侵入降级**：`LANGFUSE_PUBLIC_KEY/SECRET_KEY` 任一为空 → SDK 不初始化、
   所有埋点 no-op。没部署 Langfuse 时零内存开销、零代码分支。
2. **永不影响业务**：所有 Langfuse 调用经 `_safe()` 包裹，追踪挂了只记 warning。
   已实测：Langfuse 服务不可达时，问答正常出 421 字答案。
3. **trace id = request_id**：Langfuse 搜索框直接粘 request_id（日志、SSE 错误
   提示里都有）即可定位一次请求。
4. **流式安全**：SSE 生成器每次 yield 可能换线程，contextvar 不可靠——
   LLM generation 用手动 open/close（`open_generation` + `close`），
   单线程代码块才用上下文管理器（`span_cm`）。

### 一条 chat trace 的结构

每个节点都带 input/output，点开就能看到"这一步吃了什么、吐了什么"：

```
trace: chat  (id = request_id, input = 用户问题)
├─ span: compliance.precheck   词库+规则预检
│    in: query/user/role        out: action/blocked/layer/rule/hits 数
├─ span: sql-channel           SQL 通道（命中时）
│    in: 模板+参数              out: template_id/status  ← metadata
├─ span: retrieval             检索总耗时（= meta 事件的 took_ms）
│    in: query/top_k            out: Top3 chunk_id+分数+文档名
│    ├─ span: vector-channel   向量召回
│    │    in: query/kb_ids      out: 命中数 + Top5（分数/文档/片段）
│    ├─ span: bm25-channel     关键词召回（同上）
│    ├─ span: translate-variants  三语翻译（en/es/ar）
│    │    in: query             out: 三个译文 + degraded
│    ├─ span: variant-channels 变体检索+融合
│    │    in: 译文              out: 每语种命中数 + 融合后 Top3
│    └─ span: rerank           bge-reranker 重排
│         in: query/候选数/provider   out: before(Top3) vs after(Top5)
├─ generation: llm.stream      模型/提供商/输入 messages/输出全文/首字 ms/字数
└─ span: compliance.output     输出前语义兜底
     in: query/答案字数         out: action/blocked/changed
trace.metadata: branch / refs / total_ms / llm_provider
```

**内容瘦身规则**：chunk 全文不上报，只留 `chunk_id + 分数 + 文档名 + 80 字片段`、
每层最多 5 条。一条 trace 的体积控制在 5 KB 内，避免追踪拖慢接口。

其他追踪点：`ingest`（每篇文档索引，output = chunk/vector 统计）、
`debug.retrieval`（检索调试台）、`eval.run`（批量评测）。

## 4. 启动与使用

```bash
# 0) 首次/改过 requirements.txt 后：重建 api 镜像（含 langfuse SDK）
#    只有 COPY requirements.txt 之后这一层会重跑，本机约 7 分钟（torch+依赖全装）
docker build -t traderag-api:latest backend

# 1) 起全套（含 Langfuse；首次会跑 271 个 Prisma 迁移，约 40 秒）
docker compose --profile obs up -d --scale api=1

# 2) 打开界面
#    http://127.0.0.1:13000   admin@trade-rag.local / trade-rag-admin
#    → 项目 trade-rag-prod → Tracing → 点开最新那条 chat
# 3) 随便问一个问题，Traces 页即可看到完整链路瀑布图
```

自动化验收（不用盯界面，可重复跑）：

```bash
# 开启登录验证码时，先在容器里取一张（同一次调用里 id 与答案配对）
read CID ANS <<< $(docker exec foreign-trade-rag-api-1 python -c \
  "from app.services.captcha import new_captcha; c,_,a=new_captcha(); print(c,a)")

backend/.venv/Scripts/python.exe backend/scripts/check_langfuse_trace.py \
  --captcha-id "$CID" --captcha-text "$ANS"
# 输出：答案字数/引用数/首字延迟 + trace 树；缺关键阶段则退出码 1
```

日常开关：

- 不启动 `--profile obs`：Langfuse 完全不运行，后端埋点静默丢弃（每次 flush 失败
  记一条 warning，无其他影响）。
- 想彻底关闭：`backend/.env` 里 `LANGFUSE_ENABLED=false` 或清空两个 key。
- 压测时把 `LANGFUSE_SAMPLE_RATE` 调低（如 0.1），避免追踪洪峰拖慢接口。
- Langfuse 起来之前就在跑的 api 进程不会自动连上（key 是在进程内缓存的），
  先起 Langfuse 再 `docker compose restart api` 最稳。

## 5. 踩坑记录

1. **v2 SDK 的 trace 对象没有 `.end()`**（只有 span/generation 有），trace 由
   ingestion 侧自动定稿——封装层按节点类型区分，否则收尾时 AttributeError。
2. **pip 装的是 `langfuse==2.59.0`，但 `langfuse.__version__` 显示 2.57.13a0**——
   属性不准，以 `pip show` 为准。
3. **compose 里 LANGFUSE_INIT_* 的值不要加双引号**（官方 issue：加了会被当成
   值的一部分）。
4. **测试进程必须禁追踪**（conftest 钉 `LANGFUSE_ENABLED=false`）：否则 186 个
   用例每条都往 Langfuse 发数据，测试又慢又脏。
5. **`ENCRYPTION_KEY` 必须是 64 位 hex**（`openssl rand -hex 32`）。用 32 位
   base64 串时容器日志只报 `Invalid environment variables` + `/api/public/health`
   返回 500，Next.js 的 zod 校验把具体原因打在前面的
   `❌ ENCRYPTION_KEY must be 256 bits`——先看日志开头，别先怀疑 PG。
   改完 `docker compose --profile obs up -d langfuse` 重建容器即可（迁移已跑过不会重跑）。
6. **子 span 必须传父节点，不是整个 trace**：`retrieve(..., lf=ret_span)` 才让
   向量/BM25/翻译/重排挂在 `retrieval` 下；传 `lf`（trace）会全部平铺到根节点，
   瀑布图看不出"谁属于检索"。
7. **`update()` 别写在 `with span_cm(...)` 之后**：出块时节点已 `end()`，后置
   元数据容易被 ingestion 丢弃（模板 id、通道命中数会消失）。要在块内更新。
8. **span 只报耗时，点开就是空的**：Langfuse 的节点详情看的是 input/output，
   只写 metadata 的话用户展开子节点看不到任何内容。每个子 span 都要给
   input（query/参数）和 output（命中/分数），但必须瘦身（见 §3 瘦身规则）。

## 6. 已验证

- [x] 无 key 时全链路 no-op 冒烟（零报错、`enabled()=False`）
- [x] chat/pipeline/retrieval/m5 共 44 个用例通过
- [x] Langfuse 服务不可达：登录 + 问答正常（首字 5.4s，421 字，5 引用），只记 warning
- [x] 真机 Langfuse 2.95.11：无头初始化自动建好 org/项目/账号，预置 key 直接可用
      （`GET /api/public/projects` 返回 `trade-rag-prod`）
- [x] 端到端：真实问答（demo_sales）→ trace 9 个节点落库，父子层级正确
- [x] 每个节点都有 input/output（vector/bm25 的 Top5、译文、重排前后对照），
      单条 trace 体积 ≈ 5 KB
- [x] 改动后 chat/retrieval 回归 26 个用例通过

真机一条 trace 的实测样例（1 次问答，模型 MiniMax-M3.1-Flash）：

```
trace: chat   id=f98cf1c0dbdf4aff   user=3   latency=15.5s
  metadata: branch=kb / refs=5 / took_ms=15556 / intent=kb_question / answer_language=zh
  ├─ compliance.precheck    3 ms
  ├─ retrieval              9329 ms
    ├─ vector-channel       3162 ms   ← bge-m3 本机 CPU
    ├─ bm25-channel            6 ms
    ├─ translate-variants   3829 ms   ← 三语变体（LLM 翻译）
    ├─ variant-channels      334 ms
    └─ rerank               1994 ms   ← bge-reranker-base
  ├─ llm.stream             4145 ms   first_token_ms=12957（推理模型，首字慢）
  └─ compliance.output      2062 ms
```

追踪直接暴露了两个已知短板：**检索 9.3s 里翻译变体占 3.8s**、**首字 13s**——
这两条以前只能靠日志猜，现在在瀑布图里一眼可见。
