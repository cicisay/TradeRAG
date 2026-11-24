# 步骤 10-2：Celery + Redis 异步流水线（M6）

> 交付日期：2026-11-09 ｜ 前置：10-1 PostgreSQL 切换 ｜ 状态：✅ 完成
>
> 一句话：**上传接口从"干完活再返回"变成"登记入队毫秒返回"**，OCR / 切分 / 嵌入 / 入库全部进后台任务，
> 有台账、有进度、有重试、有取消、有队列概览，前端任务面板实时可见。

## 一、为什么必须异步（问题回顾）

上传 100 页扫描件 PDF 时，OCR 每页 0.3~1 秒，同步做会导致：

1. 浏览器转圈几分钟，Nginx 默认 60s 直接 504；
2. 用户以为失败，再点一次 → 同一文件被并发索引两遍；
3. 进程重启 → 任务消失，做到哪了没人知道。

## 二、设计

### 2.1 任务台账（ingest_job 表，迁移 0002）

| 字段 | 用途 |
| --- | --- |
| (doc_version_id, pipeline_version) 唯一 | 同一版本重复入队**复用同一行**，连点上传不会重复索引 |
| stage | queued → parsing → chunking → embedding → indexed；failed / canceled 终态 |
| progress / message | 0-100 进度 + 人话描述（"向量化 47/120"），前端直接显示 |
| attempts / max_attempts | 重试计数落库，跨进程可查，不依赖 Celery 内部状态 |
| task_id / broker | Celery task id 与执行方式（celery / thread / inline）留痕 |
| started_at / finished_at | 耗时统计（队列概览的 avg_duration_ms） |

### 2.2 三种执行模式（INGEST_MODE）

| 模式 | 何时用 | 说明 |
| --- | --- | --- |
| `auto`（默认） | 生产 | 向量库=memory → 同进程线程；向量库=milvus 且 broker 可用 → Celery |
| `celery` | 生产（Milvus 后） | 真·跨进程队列，可多 worker 横向扩容 |
| `thread` | 本机开发 | 后台线程，接口照样立即返回 202 |
| `inline` | 测试 / 排障 | 请求内同步执行完，返回 200，行为与旧版完全一致 |

**关键取舍**：`auto` 在 memory 向量库下不走 Celery——内存向量库是进程私有变量，
worker 进程里写进去的向量 API 进程**读不到**（表现为"任务成功但搜不到"）。
这不是偷懒，是把"跨进程共享向量"的正确解留给 10-3 的 Milvus，而不是用一个假队列骗自己。

### 2.3 可靠性机制（每一条对应一类真实故障）

| 机制 | 防什么 |
| --- | --- |
| 抢占 = 条件 UPDATE（CAS） | 两个 worker 同时领同一任务，只有一个成功 |
| stale 接管（900s） | worker 被 kill 后，卡在 parsing 的任务可被其他 worker 重跑（索引幂等：删旧写新） |
| 协作式取消（批次边界检查） | 不强杀线程，回滚后旧 chunk 完整保留，检索服务不中断 |
| 进度独立短会话写入 | 主事务回滚不会把"做到 60%"的痕迹一起抹掉 |
| 失败退避重试（10s → 20s → 40s…） | 解析不出文本这类**不可重试错误**直接 failed，不浪费重试 |
| acks_late + prefetch=1 | worker 崩溃任务回队列；大 OCR 任务不堵后面的小任务 |
| soft/hard time limit（30/35 分钟） | 坏文件卡死任务有上限，队列不会被永久堵住 |
| broker 不可用自动降级 thread | Redis 挂了上传照样能成功（日志留痕，不静默） |

### 2.4 接口

| 接口 | 说明 |
| --- | --- |
| `POST /api/v1/documents/upload` | 异步模式返回 **202** + `job_id`；同步模式 200 |
| `GET /api/v1/jobs/{id}` | 单任务进度（轮询用） |
| `GET /api/v1/jobs?active=true` | 进行中任务列表（面板用） |
| `GET /api/v1/jobs/stats` | 队列概览：模式、broker 可用性、各阶段数、今日完成、平均耗时、Redis 队列长度 |
| `POST /api/v1/jobs/{id}/retry` | 重试（复用同一条台账，attempts 清零） |
| `POST /api/v1/jobs/{id}/cancel` | 取消（DB 置 canceled + 尽力 revoke） |
| `POST /api/v1/system/reindex` | 默认改为批量入队（`?background=false` 保留同步排障模式） |

权限：全部要求 `kb:manage`，且按业务组过滤（b 组管理员看不到 a 组任务，越权统一 404）。

### 2.5 前端（knowledge 页）

- 上传卡：上传后显示任务进度条（阶段 + 百分比 + 当前动作），完成自动刷新文档列表；
- 新增「处理任务」面板：最近 8 条任务、进度条、重试/取消按钮、队列模式徽章（celery 模式还显示 Redis 队列长度）；
- 每 4 秒低频刷新 + 上传后 1.2 秒高频轮询，数据签名不变不重渲染（避免闪烁）。

## 三、实测结果

| 验证 | 结果 |
| --- | --- |
| 自动化测试 | **146 passed, 1 skipped**（新增 14 条队列用例） |
| Celery 真实链路（`scripts/queue_smoke.py`） | API+独立 worker 双进程，上传 **202 / 418ms**，轨迹 queued→embedding 88%→indexed 100%，attempts=1，broker=celery，task_id 留痕 |
| 浏览器实测（playwright） | 页面上传 → 进度条 88% → 已解析，任务面板出现记录并可取消/重试，**控制台 0 报错** |
| 迁移往返 | 0002 升级 → 回滚 → 再升级，三连通过 |
| 幂等 | 同版本重复入队不新增台账行（测试覆盖） |
| 取消安全性 | 协作式中止后回滚，旧 chunk 完整保留（测试覆盖） |

## 四、踩坑记录（6 个，均已修复）

1. **redis-py 8 默认 RESP3**：握手先发 `HELLO 3`，Redis 5/6 直接报 `unknown command HELLO`。
   应用层显式 `protocol=2` 解决；但 **kombu 的连接参数是白名单，传不进 protocol** → 最终把 redis-py 锁在 6.4.0（默认 RESP2）。requirements.txt 里写明了原因。
2. **Redis 5.0 Windows 移植版跑不了 kombu 5.7 的 Lua 脚本**（`ERR syntax error`，脚本用了新语法）。
   换 **Redis 8.10.3 Windows 构建**（redis-windows 社区版，`scripts/local_redis.py` 一键下载启动）。
3. **Celery result backend 是多余的**：任务进度/结果以台账为准，backend 只会多一条 pubsub 连接
   （还触发了上面第 1 条的 RESP3 崩溃）→ 去掉 backend，少一类故障源。
4. **queued 判定错误**：解析失败的任务曾返回 202 + `queued=true`，前端会傻轮询一个永远不会变的任务 → 终态（failed/canceled）不算排队。
5. **run_worker.py 直接跑报 No module named 'app'**：scripts 目录不在 sys.path → 启动前显式插入项目根。
6. **测试脚本 localStorage 陷阱**：8010 端口登录的 token 在 8000 端口无效（origin 隔离），
   浏览器检查脚本必须在目标域内登录。

## 五、怎么用

```bash
# 本机（Windows，免 Docker）
python scripts/local_redis.py up          # 下载并启动 Redis 8（端口 56379）
set INGEST_MODE=celery && set VECTOR_STORE=milvus   # 接 Milvus 后才建议
python scripts/run_worker.py              # 启动 worker（Windows 自动 solo 池）
python scripts/queue_smoke.py             # 端到端冒烟（起 API+worker，全程自动）

# 现在的默认行为（本机，memory 向量库）
uvicorn app.main:app --port 8000          # 上传即返回 202，后台线程执行，进度可见
```

生产（Linux）：Redis 7 官方包 + `celery -A app.worker.celery_app:celery_app worker -Q ingest -c 4`，
配 systemd/supervisor 守护；`INGEST_MODE=celery`、`VECTOR_STORE=milvus`。

## 六、已知边界（进入 10-3 前必须知道）

1. **memory 向量库 + Celery = 任务成功但检索不到**（跨进程不共享）。代码已用 `auto` 模式规避，
   强制 `INGEST_MODE=celery` 时这是预期行为，切 Milvus 后消失。
2. **取消是协作式的**：OCR 一页进行中无法打断，最多延迟一个批次（秒级）。
3. **worker 崩溃的任务**靠 stale 接管（默认 900 秒），不是秒级恢复。
4. **Windows solo 池 = 一次一个任务**：并发吞吐验证要等 10-4 的 Linux 多副本压测。
5. SQLite 下的进度写入与主事务并发依赖 WAL + busy_timeout，PG 下无此限制。
