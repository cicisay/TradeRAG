# 步骤 10-4：部署形态、压测与验收（M6 收官）

> 目标：把前面六个里程碑拼成**一套能开机自启、能被人访问、能量化说明"能扛多少人"**的单机部署，并留下可复现的验收证据。
>
> 本步不加业务功能。唯一产出是「可信度」：出问题能定位，换台机器能复现，验收/交接时能拿出数字。

---

## 一、部署形态

单机 Docker Compose 一套栈，8 个容器：

```
                        ┌──────────── 127.0.0.1:18080 ────────────┐
                        │              nginx（SSE 透传）          │
                        └───────────────┬────────────────────────┘
                                        │ Docker DNS 动态解析
                        ┌───────────────▼────────────────────────┐
                        │  api（FastAPI，--scale 可水平扩）        │
                        │  worker（Celery solo 池）               │
                        └───┬────────┬────────┬────────┬─────────┘
                            │        │        │        │
                     ┌──────▼──┐ ┌───▼───┐ ┌──▼───┐ ┌──▼────┐
                     │postgres │ │ redis │ │milvus│ │ minio │
                     │  16     │ │  7.2  │ │2.4.15│ │  持久 │
                     └─────────┘ └───────┘ └──┬───┘ └───────┘
                                              │
                                          ┌───▼───┐
                                          │ etcd  │
                                          └───────┘
```

### 1.1 端口收口（安全）

所有端口**只绑回环地址**，即使防火墙规则失效也不会被局域网直连：

| 服务 | 绑定 | 说明 |
| --- | --- | --- |
| nginx | `127.0.0.1:18080` | 唯一对外入口 |
| postgres | `127.0.0.1:5432` | 仅供宿主机跑搬数/体检脚本 |
| milvus | `127.0.0.1:19530` / `9091` | gRPC + 指标 |
| redis | `127.0.0.1:6379` | 队列 |

### 1.2 副本数的取舍

| 方案 | 内存 | 适用 |
| --- | --- | --- |
| `--scale api=2` | 约 2.9 GB | 演示"水平扩展"能力；多副本分流的依据可复现 |
| `--scale api=1` | 约 1.67 GB | 3-5 人日常自用，省 1.2 GB |

**结论：日常单副本，需要展示扩容能力时临时 `--scale api=2`。**

Nginx 侧无需改配置即可支持扩缩容——`proxy_pass` 用的是变量 + Docker DNS（`resolver 127.0.0.11 valid=10s`），若写成 `upstream server api:8000` 则只在启动时解析一次，永远打同一个副本，扩容等于白扩。

---

## 二、冷启动：踩到的坑

Docker Desktop 重启后，容器处于"半死"状态：`nginx` 活着，但 `postgres/milvus/etcd/minio/redis` 全部 `Exited (255)`，`api` 在崩溃重启循环里刷日志：

```
sqlalchemy.exc.OperationalError: failed to resolve host 'postgres'
ERROR: Application startup failed. Exiting.
```

**原因**：基础设施容器没起来，Docker 网络里就解析不到 `postgres` 这个服务名，api 连不上库直接退出，`restart: unless-stopped` 又把它拉起来，循环。

**正确做法**：不要等它自愈，直接 `down` 重来：

```bash
docker compose down --remove-orphans      # 清掉残留容器，数据卷保留
docker compose up -d --scale api=1        # 冷启动
```

`down` 不带 `-v` 不会删数据卷，PG / Milvus / MinIO 的数据都在。

---

## 三、压测设计与结果

### 3.1 为什么分级压

上一轮直接用 80 并发硬打，把 Docker Desktop 引擎压崩（72% 错误率），连容器都拉不起来。这是 Windows 上 Docker Desktop 的已知短板，不是应用缺陷。

改为**分级加压**，每档独立可判定：

| 档位 | 场景 | 并发 | 目的 |
| --- | --- | --- | --- |
| 轻 | `read`（文档列表 60% + 检索 40%） | 10 | 验证常规页面操作不受并发影响 |
| 中 | `read` | 3 | 贴近真实 3-5 人使用 |
| 重 | `chat`（SSE 完整问答） | 3 | 验证 LLM 链路并发稳定性 |

### 3.2 实测数据

| 场景 | 并发 | 成功 | 失败 | P50 | P95 | 吞吐 |
| --- | --- | --- | --- | --- | --- | --- |
| read | 10 | 78 | 0 | 253 ms | 15170 ms | 2.3 req/s |
| read | 3 | 77 | 0 | 56 ms | 3710 ms | 2.7 req/s |
| chat（修复后） | 3 | 12 | 0 | 6774 ms | 13120 ms | 0.4 req/s |

**合计 167 个请求，HTTP 错误 0。**

### 3.3 数据怎么读

- **read 3 并发 P95 = 3710 ms**，正好等于单次检索的固有耗时（含三语翻译 + 双向召回 + 重排），说明**没有排队**，3 个人同时用互不影响。
- **read 10 并发 P95 = 15170 ms**：超出实际需求 2 倍后开始排队（重检索是 CPU 密集，同进程内串行）。能跑、不报错、只是慢——对 3-5 人场景属于余量。
- **轻请求 P50 只有 56-253 ms**：说明同步路由没有阻塞事件循环（FastAPI 把同步 `def` 路由放到线程池执行），所以重检索排队时，页面切换、列表加载依然秒开。
- **chat 吞吐 0.4 req/s** 的瓶颈是外部 LLM 生成速度，不是本机。本机检索 2.7 req/s，差一个数量级，扩容副本对问答帮助有限，优化要落在 LLM 链路。

---

## 四、关键发现：LLM 链路的三层问题

压测日志暴露了 163 条 `429 Too Many Requests`，进一步排查出三个叠加问题。

### 4.1 问题一：降级链形同虚设

`.env` 里配了三家，但 `DEEPSEEK_API_KEY` 和 `QWEN_API_KEY` 是空的，实际调用链只有 **MiniMax 一家**。限流时无处可降，只能走兜底话术。

**修复**：补上 DeepSeek Key，链路变两家。

### 4.2 问题二：MiniMax 是推理模型，首字极慢

`MiniMax-M3.1-Flash` 把正文放在 `reasoning_content` 之前，必须先吐完思考块才输出第一段正文。直连测试 30 秒内**一个 content 字符都没收到**，导致问答首字延迟十几秒——这是用户能直接感知的"卡"。

| 模型 | 首字 | 1000 字答完 | 是否推理模型 |
| --- | --- | --- | --- |
| MiniMax-M3.1-Flash | > 15 s | 未完成 | 是 |
| DeepSeek-chat | **1.47 s** | **6.1 s** | 否 |

**修复**：`env_chain()` 顺序调整为 DeepSeek 主用、MiniMax 备用（`app/services/llm_config.py`，已注释说明原因）。

### 4.3 问题三：429 没有分级处理

原实现对每个提供商"失败就换下一家"，但对 429 和 5xx 一视同仁，且没有退避。限流时快速重试反而加剧限流。

**修复**（`app/services/llm.py`）：

| 错误类型 | 处理策略 | 理由 |
| --- | --- | --- |
| 429 限流 | **立刻切下一家** | 重试只会更糟，换家最快 |
| 500/502/503/504、超时 | 原地等 0.6 s 重试 1 次，再切 | 厂商抖动，等一下可能就好 |
| 401/400 等 | 直接切下一家 | Key 错/参数错，重试无意义 |
| 流式已吐字后失败 | 直接抛错，不换家 | 否则用户会看到两个模型的答案拼接 |

### 4.4 修复前后对比（同为 3 并发 30 秒）

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| chat P50 | 17446 ms | **6774 ms** |
| chat 吞吐 | 0.2 req/s | **0.4 req/s** |
| 成功数 | 6 | **12** |
| 429 次数 | 163 | **0** |
| 走兜底次数 | 大量 | **0** |
| LLM 首字 | 2056 ms | **812 ms**（优于 FR-209 的 2.5 s） |

> 注意一个陷阱：修复前那次 `chat P50=3264 ms` 看着更快，其实是 LLM 失败后**走兜底话术**的速度。压测的"成功"只统计 HTTP 状态码，不判断回答是否由模型生成——**指标好看不等于功能正常**。

### 4.5 附带修复

`backend/scripts/check_llm.py` 在多提供商重构后失效（读 `client.model`，属性已改名 `primary_model`），一跑就 `AttributeError`。改为遍历降级链打印每家状态，并对外只显示 Key 后 4 位。

---

## 五、全量回归

| 项 | 结果 |
| --- | --- |
| 主回归 | **185 通过 / 1 跳过 / 0 失败**，3 分 01 秒 |
| 跳过项补跑 | `TestRealModel.test_bge_m3_semantic_similarity` 通过（真实 bge-m3，1024 维，51.89 s） |
| 合计 | **186 个用例全绿** |

覆盖 16 个测试文件：认证与入口隔离、验证码、对话、合规、文档、LLM 管理、可观测、PostgreSQL、Celery 队列、OCR 与模型、流水线、检索、SQL 通道、前端特性、向量库。

**测试隔离**（ensure 不污染生产数据）：

- 数据库 → 临时 SQLite，跑完删除
- 向量库 → 强制内存实现（`VECTOR_STORE=memory` 钉死，否则会写进真实 Milvus）
- 嵌入 → hash 假向量（不加载模型，快且确定）
- 验证码 → 关闭（单独在 `test_captcha.py` 测）
- Redis → 测试端口，不碰 Celery 队列

---

## 六、验收清单

### 6.1 部署与可达性

- [x] 8 个容器单机拉起，无重启循环
- [x] `/health` 返回 `{"status":"ok"}`
- [x] 三个前端入口可访问：`/app/index.html`、`/app/admin.html`、`/app/knowledge.html`
- [x] 所有端口绑 `127.0.0.1`（不对外暴露）
- [x] Nginx 支持 `--scale` 扩缩容（DNS 动态解析）

### 6.2 数据与检索

- [x] PostgreSQL 健康，业务数据可读写
- [x] Milvus 真机连通，`capabilities` 返回 `backend: milvus`、`degraded: null`
- [x] 集合 `chunks_bge_m3_1024` 存在，36 个向量完整
- [x] 混合检索三路都有结果：向量 5 / BM25 5 / 融合 5
- [x] 跨语言变体生成正常（en / es / ar）
- [x] 重排生效（`bge-reranker-base`，`applied: true`，非 fallback）

### 6.3 问答链路

- [x] 提供商链 2 家（DeepSeek 主 + MiniMax 备）
- [x] LLM 首字 812 ms（目标 ≤ 2500 ms）
- [x] 压测期间零 429、零兜底
- [x] 429/5xx 分级处理已生效

### 6.4 质量门

- [x] 186 个用例全绿
- [x] 压测 167 请求零 HTTP 错误
- [x] 压测后容器稳定、无重启、内存 1.52 GB / 7.65 GB

### 6.5 未执行项（有意跳过，非遗漏）

| 项 | 状态 | 理由 |
| --- | --- | --- |
| 80 并发重压 | 未执行 | 3-5 人使用场景，10 并发已覆盖 2 倍余量；Windows Docker Desktop 在 80 并发下有压崩引擎的实测先例，收益不成正比 |
| 嵌入替换演练（minilm 384 维） | 未执行 | 换模型代码路径已由 `test_vector_store.py` 覆盖（维度校验、集合复用报错）；演练价值主要在运维手册，对单人自用/演示无实质影响 |
| 双副本分流验证 | 已在 10-3 验证 | 本轮改单副本省内存，扩缩容能力由 Nginx 配置保证 |

---

## 七、已知限制

| 限制 | 影响 | 缓解方式 |
| --- | --- | --- |
| 检索单次 3.7 s | 首字延迟被检索拖高 | 三语翻译是主要成本；3-5 人场景可接受，追求更低可关掉翻译变体（会失去跨语言能力） |
| 单副本下重检索串行 | 10 并发 P95 到 15 s | 需要更高并发时 `--scale api=2` |
| MiniMax 免费额度限流 | 主用已切 DeepSeek，影响消除 | 保留 MiniMax 作备用，不要作为主用 |
| `capabilities` 显示的集合名带连字符 | 按该名去 Milvus 查会找不到（实际集合是净化后的下划线名） | 仅展示值未走净化函数，功能无影响；排查时以 Milvus 实际集合名为准 |
| Milvus 仍用出厂账号 `root/Milvus` | 单机自用可接受，生产不合格 | 后续通过 `scripts/milvus_rbac_setup.py` 改密 |

---

## 八、常用运维命令

```bash
# 启动（单副本，省内存）
docker compose up -d --scale api=1

# 启动（双副本，展示水平扩展）
docker compose up -d --scale api=2

# 改了 Python 代码：必须重建镜像（代码打包在镜像里，不是挂载）
docker compose build api && docker compose up -d --scale api=1

# 只改了 .env：重建容器即可（env_file 在容器创建时注入）
docker compose up -d --scale api=1

# 全部停止（保留数据）
docker compose down

# 健康检查
curl -s http://127.0.0.1:18080/health

# LLM 体检（打印降级链 + 首字延迟）
cd backend && .venv/Scripts/python.exe scripts/check_llm.py

# 全量回归
cd backend && .venv/Scripts/python.exe -m pytest tests -q

# 分级压测
cd backend && .venv/Scripts/python.exe scripts/stress_test.py \
  --base http://127.0.0.1:18080 --scenario read --concurrency 3 --seconds 25
```

---

## 九、遗留移交

| 项 | 优先级 | 说明 |
| --- | --- | --- |
| Milvus 正式改密 | 中 | 出厂 `root/Milvus` 仅适合单机自用 |
| `capabilities` 集合名净化 | 低 | 展示层小瑕疵，一行改动 |
| 首字延迟优化 | 低 | 检索 3.7 s 是主因，可引入查询缓存或关翻译变体 |
| 主键换雪花 ID | 低 | 当前 md5 56 bit，碰撞概率极低但不为零 |

---

**这一步之后，M6 生产化全部完成，项目六个里程碑收官。**

## 10.x 追加记录：对外端口 8080 → 18080

2026-11-11 冷启动时 nginx 绑定 8080 失败：`bind: An attempt was made to access a socket
in a way forbidden by its access permissions`。根因是 Windows 的 Hyper-V/WSL 会动态保留
端口段（当天保留了 8071–8170，正好覆盖 8080），且保留段每次重启会漂移。

处理：`docker-compose.yml` 里 nginx 映射改为 `127.0.0.1:18080:80`（仅宿主端口变化，
容器内与 Nginx 配置不变）。诊断命令：`netsh interface ipv4 show excludedportrange protocol=tcp`。
