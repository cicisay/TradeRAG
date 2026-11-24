# 11-3 管理后台链路追踪模块

把 Langfuse 里的 trace 搬进管理员后台，日常排查不用开第二个网站、登第二个账号。
**定位是只读镜像**：按 session 聚合、成本趋势、评分分析这类仍然跳 Langfuse。

- 效果预览：`docs/design/admin-trace-module.html`
- 入口：`http://127.0.0.1:18080/app/admin.html` → 链路追踪（需 `audit:read`）
- 上线日期：2026-11-17

## 1. 为什么是后端代理，不是前端直连

| 方案 | 结论 | 原因 |
|---|---|---|
| 前端直连 `127.0.0.1:13000` | ✗ | Langfuse 端口没有经过 nginx；secret key 必须下发到浏览器，等于公开密钥 |
| iframe 嵌入 Langfuse | ✗ | 跨域 + 需要单独登录态；深色 UI 与后台风格割裂，手机端不可用 |
| **后端代理 + 字段投影** | ✓ | key 留在服务端；可按后台样式重绘；Langfuse 停机时能优雅降级 |

```mermaid
flowchart LR
  A[admin.html\n链路追踪 tab] -->|GET /api/v1/admin/traces| B[FastAPI 代理\napp/api/traces.py]
  B -->|GET /api/public/traces| C[Langfuse :3000\n容器内网]
  A -->|GET /api/v1/admin/traces/{id}| B
  B -->|GET /api/public/traces/{id}| C
  B -.未配置/连不上.-> D[返回 200 + reachable=false]
  D --> A
```

三个必须绕开的坑：

1. **列表接口只给节点 ID**。`/api/public/traces` 返回的 `observations` 是 uuid 数组，
   节点内容要再请求 `/traces/{id}`。所以交互设计成**选中哪条才拉哪条**，
   不会一次拉 50 条 × 9 节点。
2. **Langfuse 是可选栈**（`profiles: ["obs"]`）。没启动时必须返回 200 + 空状态，
   不能抛 500，否则后台一进这个 tab 就像模块坏了。
3. **observation 的 input/output 可能几 KB**。一页 50 条能撑到几 MB，
   前端渲染直接卡死。统一截断到 4000 字并标注「已截断」。

## 2. 接口契约

### `GET /api/v1/admin/traces`

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `hours` | int | 24 | 回看多少小时（1–720） |
| `limit` | int | 50 | 条数上限（≤100） |

```json
{
  "enabled": true, "reachable": true, "reason": "", "total": 10, "hours": 24,
  "items": [{
    "id": "ec186c4cab6d4085", "name": "chat",
    "timestamp": "2026-11-16T13:37:37.372Z", "latency": 22.118,
    "userId": "3", "sessionId": "84",
    "branch": "kb", "intent": "kb_question", "refs": 5, "took_ms": 22138,
    "question": "不锈钢法兰的材质和压力等级有什么要求？",
    "nodes": 9, "level": "DEFAULT", "scores": 0
  }]
}
```

三种非正常返回（都是 HTTP 200，前端据此渲染空状态）：

| 场景 | `enabled` | `reachable` | `reason` |
|---|---|---|---|
| 未配 key | `false` | `false` | 未配置 Langfuse（LANGFUSE_PUBLIC_KEY / SECRET_KEY 为空） |
| 服务没起 | `true` | `false` | 连不上 Langfuse，追踪服务未启动 |
| key 不匹配 | `true` | `false` | Langfuse 拒绝了请求，public/secret key 不匹配 |

### `GET /api/v1/admin/traces/{trace_id}`

返回 `trace`（头信息 + input/output）与 `observations`（扁平静态数组，
父子关系靠 `parentId`，由前端构树）。单个 input/output 超 4000 字截断。

## 3. 前端实现

- 位置：`frontend/assets/admin.js` 的 `renderTrace()`，tab id `trace`，权限 `audit:read`
- 样式：`frontend/assets/app.css` 末尾，前缀 `tr-`（列表）、`tw-`（瀑布图），
  与既有 `.bar` / `.rank` 无冲突
- 构树：`buildTraceTree()` 按 `startTime` 排序后 DFS，深度即缩进层级
- 瀑布：DOM 绝对定位。`left = 起点/总时长`、`width = 耗时/总时长`，
  最小宽度 4px 保证 `compliance.precheck`（4ms）可见
- 首字线：取 `GENERATION` 节点的起点位置画青色虚线。**不直接用
  `metadata.first_token_ms`**——那个值是相对请求开始计的，与 generation 的
  `startTime` 口径不一致，直接画会错位
- 过滤（分支 / 仅 >10s / 关键词）在浏览器本地做，不来回打接口

## 4. 回退（重要）

项目不是 git 仓库，所以用文件快照回退。

**改动前已备份**到 `.snapshot/trace-module-20261117-093237/`：

```
frontend/admin.html
frontend/assets/admin.js
frontend/assets/app.css
backend/app/main.py
```

**一键回退**：双击项目根目录的 `rollback-trace-module.bat`，确认后自动
恢复上述 4 个文件，并删除新增的 `backend/app/api/traces.py`。

回退后还要让容器生效：

```bash
docker build -t traderag-api:latest backend
docker compose --profile obs up -d --scale api=1
```

**只删不恢复也可以**：这个模块是纯增量（新增一个 tab、一个路由文件），
把 `TABS` 里那一行删掉、`main.py` 里那一行 include 删掉，功能就消失了，
不影响其他 8 个模块。`tests/test_trace_module.py` 是新增文件，可一并删除。

## 5. 验收清单

| 项 | 怎么验 | 状态 |
|---|---|---|
| 未配置 key 时显示「追踪服务未配置」 | 清空 `.env` 的两个 key，重启 api | ✅ 单测覆盖 |
| Langfuse 停机时显示「追踪服务未启动」 | `docker compose stop langfuse` | ✅ 单测覆盖 |
| 字段投影与截断正确 | `pytest tests/test_trace_module.py` | ✅ 7 passed |
| 列表能拉到真实 trace | 打开后台 → 链路追踪 | ⏳ 待 Docker |
| 瀑布图起止位置与 Langfuse 一致 | 与 13000 界面逐条对照 | ⏳ 待 Docker |
| 点节点能看到 input/output | 点 `vector-channel` | ⏳ 待 Docker |
| 越权账号看不到 tab | `demo_sales` 登录后台 | ⏳ 待 Docker |
| 现有 8 个模块不受影响 | 逐个点一遍 + 全量回归 | ⏳ 待 Docker |

## 6. 已知边界

1. **拿不到 token 与成本**：usage 在 observation 里，列表接口不返回节点内容。
   列表页只显示耗时、分支、引用数、节点数；token 在详情页头部显示（取自
   generation 的 usage）。
2. **分页只做一层**：一次最多 100 条，没有无限滚动。数据量大时用时间范围收窄，
   真要看历史去 Langfuse。
3. **没有实时刷新**：避免自动刷新把用户正在看的选中项冲掉。手动点刷新。
4. **不能改数据**：这是只读镜像，标注、评分、打标签仍在 Langfuse 做。
5. **多副本不适用**：代理走的是 `langfuse_host`，如果 Langfuse 扩成多副本，
   需要走它的 load balancer，当前单副本没问题。
