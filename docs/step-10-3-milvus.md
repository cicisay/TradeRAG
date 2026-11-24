# 步骤 10-3：Milvus 真机接入（M6）

> 交付日期：2026-11-10 ｜ 前置：10-2 异步流水线 ｜ 状态：✅ 完成
>
> 一句话：**向量检索从"进程内存兜底"切到"真 Milvus"**——Docker Compose 拉起
> etcd + MinIO + Milvus v2.4.15 Standalone，`VECTOR_STORE=milvus` 一键切换，
> 25 篇文档 36 条向量重建入列，检索、权限过滤、越权隔离、异步摄入全链路验证通过。

## 一、环境打通（本步最大的时间花在这里）

| 事项 | 结果 |
| --- | --- |
| Docker Desktop | 已就绪（引擎 29.9.2） |
| pymilvus | 清华镜像源无 2.4.x 且不支持 Python 3.13 → 官方源装 **pymilvus 2.5.14**（2.5 客户端向下兼容 2.4.15 服务端） |
| Docker Hub | 国内直连被墙（EOF）→ 镜像走 **DaoCloud**（milvus/etcd/redis）+ **华为 SWR ddn-k8s**（minio，DaoCloud 对 minio 命名空间返回 denied）后 `docker tag` 改回原名 |

容器启动后 `pymilvus.MilvusClient("http://127.0.0.1:19530")` 连通验证 OK。

## 二、切换配置

```ini
# backend/.env
VECTOR_STORE=milvus
MILVUS_URI=http://127.0.0.1:19530
REDIS_URL=redis://127.0.0.1:6379/0   # 换用 compose 里的容器 Redis
INGEST_MODE=auto                      # milvus + broker → Celery 异步摄入
```

`requirements.txt` 启用 `pymilvus==2.5.14`。启动顺序：`docker compose up -d` → API → `python scripts/run_worker.py`。

## 三、真机联调抓出的 3 个真 bug（全部修复）

| # | 问题 | 修复 |
| --- | --- | --- |
| 1 | **collection 名非法**：`chunks_bge-m3_1024` 带连字符，Milvus 报 E1100（只允许字母数字下划线），25 篇全部写入失败 | `vector_store.py` 新增 `sanitize_collection_name()` 统一净化（`bge-m3 → bge_m3`），pipeline / retrieval 全部走它；测试与 pipeline 同源后回归通过 |
| 2 | **主键碰撞 + 解析崩溃隐患**：旧写法 `int(content_hash.replace("-","")[:16])` 把 `"5-12-3"` 与 `"51-2-3"` 拼成同一个数（静默覆盖向量），且出现非数字即抛异常 | 改为 `int(md5(content_hash)[:14], 16)`——56bit 确定性整数，无碰撞风险 |
| 3 | **写入不可见窗口**：Milvus 默认 Bounded 一致性，insert 后立刻检索可能查不到新向量（异步摄入完成瞬间用户提问就命中不了） | `insert()` 末尾显式 `flush()` |

## 四、验证记录

### 4.1 索引重建
`scripts/reindex.py`：25 篇成功 0 失败，35 块入列；Celery 异步上传 doc 26 后行数 35→36，worker 日志确认 `store=milvus vectors=1`。

### 4.2 检索与权限隔离（Milvus 库端强制过滤）
- demo_sales（biz=default）问 "DAP 和 DDP"：Top1 命中单证合规库 score 0.66；
- demo_sales_b（biz=b）只命中 biz_b 文档；
- **越权探测**（biz_b 用户查 default 组库）→ 0 命中。

### 4.3 端到端 SSE 问答
demo_sales 登录 → 提问 "出口越南凭 Form E 享受什么税率"：
- 事件流 `stage×3 → meta → delta×10 → done`，流式正常；
- Top1 引用即刚上传的 `rcep_rules_test.txt`（异步摄入完成即刻可检索，验证 flush 修复）；
- 回答带 `[1]~[5]` 引用标注，事实正确（Form E 协定税率 / BOM+发票+工序 / 60% 累计规则）；
- `llm_provider=minimax`。

### 4.4 回归
全量 pytest **185 passed, 1 skipped**（新增 `tests/test_vector_store.py` 6 例）。

## 五、安全自查（本步新增自查环节，发现 3 处）

| # | 风险 | 严重度 | 处置 |
| --- | --- | --- | --- |
| 1 | **Milvus/Redis 端口绑 0.0.0.0**：两者均无认证，同网段主机可直连读写向量库、投递队列任务 | 高 | compose 改绑 `127.0.0.1:`，etcd/MinIO 本就未暴露；`docker compose down && up` 重建，数据在卷中无损 |
| 2 | **测试隔离漏洞**：`config.py` 会读 `.env`，夹具未钉 `VECTOR_STORE`，全量测试会把测试向量写进真实 collection | 高 | `tests/conftest.py` 强制 `VECTOR_STORE=memory` + 测试专用 `REDIS_URL`；验证测试跑完真实库仍 36 行 |
| 3 | **expr 注入面**：`build_access_expr` 对 `biz_group` 未转义双引号，恶意组名可拼出 `or 1==1` 越权表达式 | 中 | 反斜杠/双引号转义后拼入（`tests/test_vector_store.py` 有回归用例） |

## 六、遗留与移交 10-4

1. **Milvus 认证未启用**：本机开发可接受；生产部署（10-4）应启用 Milvus RBAC + MinIO 改密。
2. **主键仍是 md5 短哈希**：千万级以上规模建议换雪花 ID（代码注释已标）。
3. **Milvus 运行中宕机**：首次连接失败会静默降级内存库（有 warning 日志），运行中宕机则检索报错——10-4 部署验收时定重试/降级策略。
4. `docker-compose.yml` 的 `api` 服务注释块保留，10-4 启用做完整容器化部署。
