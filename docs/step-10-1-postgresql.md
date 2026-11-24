# 步骤 10-1：切换到 PostgreSQL（M6 第一步）

> 目标：把"本地演示能跑"升级成"生产能托付"。数据库从 SQLite 换成 PostgreSQL，
> 并且**真的把数据搬过去、真的在 PG 上跑通了问答**，而不是只改一行连接串。

---

## 一、这一步到底做了什么（小白版）

把系统想象成一家门店，数据库就是仓库。

- **SQLite** 像店里的小储物间：一个文件、开箱即用、一个人用很爽，但**同时只能一个人进去搬货**（写锁），
  而且没有"管理员权限"概念，谁拿到文件谁就能改。
- **PostgreSQL** 像正规仓库：多个人同时进出、有门禁和权限、有监控、断电不会丢货。

这一步做了四件事：

1. **换仓库门牌**：`DATABASE_URL` 从 `sqlite:///...` 改成 `postgresql+psycopg://...`，
   代码不需要改任何一行业务逻辑。顺手把"连接池"配好——这是 PG 和 SQLite 最大的运维差异。
2. **统一时间口径**：两种数据库存时间的方式不一样，SQLite 会**悄悄丢掉时区**。
   我们做了一个统一类型，保证"写进去是 UTC，读出来还是 UTC"，不管底层是哪家数据库。
3. **给仓库装上"施工图纸管理"**（Alembic）：以后改表结构（加字段、加索引）不再靠
   "删库重建"，而是像 git 一样一版一版升级，可回滚、可审计。
4. **搬货**：写了一个迁移脚本，把 SQLite 里的 320 行数据（用户、文档、分片、会话、
   合规事件、评测记录……）原样搬进 PG，并且**重置自增序列**——这一步不做，
   迁移后第一次新建用户就会主键冲突。

---

## 二、改了哪些文件

| 文件 | 作用 |
| --- | --- |
| `app/db/types.py`（新增） | `TZDateTime`：跨库统一时间类型（进出一律 UTC aware） |
| `app/db/session.py` | 连接池、`pool_pre_ping`、`statement_timeout`、SQLite 外键开关、`db_info()` 自检 |
| `app/config.py` | 新增 `DB_POOL_*` / `DB_AUTO_CREATE` / `DB_STATEMENT_TIMEOUT_MS` 等 8 个配置 |
| `app/models/*.py`（8 个） | 时间列从 `DateTime(timezone=True)` 换成 `TZDateTime` |
| `app/api/badcases.py`、`app/api/monitor.py` | `datetime.utcnow()`（naive）→ `datetime.now(timezone.utc)`（aware） |
| `app/api/system.py` | 新增 `GET /api/v1/system/db`：一眼看清连的哪个库、时区口径、连接池占用 |
| `alembic.ini` + `migrations/`（新增） | 迁移框架，初始版本 `0001_initial_schema.py`（18 张表 + 40 个索引） |
| `scripts/migrate_sqlite_to_pg.py`（新增） | 数据搬迁：类型转换、外键顺序、序列重置、行数校验、`--dry-run` |
| `scripts/check_db.py`（新增） | 数据库体检：缺表、序列落后、时间列类型、约束数量 |
| `scripts/local_pg.py`（新增） | 本机起一个临时 PG（免安装二进制，端口 55432，不污染系统） |
| `scripts/verify_pg.py`（新增） | 一条命令跑完：起库 → 建表 → 搬家 → 体检 → 探针 |
| `tests/test_m6_pg.py`（新增） | 20 条用例：时区、PG 方言、值转换、自检接口、迁移完整性 |

---

## 三、四个关键设计（为什么这么做）

### 1. 时间：为什么一定要自定义类型？

SQLite 的"时间"本质上是一段文本。SQLAlchemy 写入时会把时区信息丢掉，读出来是
"没有时区的时间"（naive）。而 PostgreSQL 的 `timestamptz` 读出来是"带时区的时间"（aware）。

于是出现两个坑：

- **报错**：`datetime.now(timezone.utc) > row.created_at`（aware 比 naive）→ `TypeError`。
- **更危险的是不报错**：两边都 naive，程序照跑，但把 UTC 当成了服务器本地时间，
  于是"7 日知识缺口榜"少算 8 小时、"今日指标"跨天点错位——这种错不会抛异常，只会悄悄算错。

`TZDateTime` 的做法：写库前一律转 UTC，读库后如果没时区就补 UTC。
在 PG 上它仍然映射成原生 `TIMESTAMP WITH TIME ZONE`，不损失任何能力。

同时把代码里 4 处 `datetime.utcnow()` 换成 `datetime.now(timezone.utc)`——
Python 3.12 起 `utcnow()` 已标记弃用，且返回 naive，会和新类型打架。

### 2. 连接池：为什么 PG 必须配，SQLite 不用？

- SQLite 是"打开一个本地文件"，开销接近零，并发靠文件锁，池子意义不大。
- PG 是网络服务，一次建连要走 TCP + 认证，几十毫秒。**每个请求新建连接**的写法，
  在 50 并发时会瞬间打出几百个连接，把 PG 的 `max_connections` 打满，
  后续所有请求都报 `too many clients`——这是上线第一天最常见的故障。

配置：常驻 10 + 峰值 20（`pool_size` / `max_overflow`），借不到连接最多等 10 秒
（`pool_timeout`，快速失败而不是无限挂起），连接最长复用 30 分钟（`pool_recycle`，
防止被防火墙静默断开），借出前探活（`pool_pre_ping`，自动换掉死连接）。

另外在连接上设了两条会话参数：
`timezone=UTC`（保证读出来的时间永远以 UTC 呈现）和
`statement_timeout=10s`（慢查询兜底，避免一条烂 SQL 把池子占死）。

### 3. 外键：SQLite 的一个历史坑

SQLite **默认不检查外键**（为了兼容 2001 年的老库）。这意味着本地测试时
"删掉角色但用户还引用着"不会报错，一上 PG 立刻报 `violates foreign key constraint`。

所以对 SQLite 连接显式执行 `PRAGMA foreign_keys=ON`，让本地和生产的约束行为一致。
顺手开了 `WAL` 模式，读写并发更接近 PG 的体验。

### 4. 迁移：三个必踩的坑，脚本里都处理了

| 坑 | 现象 | 处理 |
| --- | --- | --- |
| **类型不兼容** | SQLite 的布尔是 `0/1`，PG 的 `boolean` 只认 `true/false`，第一行就报错 | 按目标列类型逐值转换 |
| **JSON 双重编码** | 源库 JSON 列存的是文本 `'["zh"]'`，原样插入 PG 的 JSON 列会被再 dumps 一次，变成 `'"[\"zh\"]"'`——有数据但程序读出来是字符串 | 先 `json.loads` 再交给 PG |
| **序列没重置** | 保留原 id 插入后，PG 自增序列还停在 1，**迁移后第一次新建用户就主键冲突** | 每张表执行 `setval(pg_get_serial_sequence(...), max(id))` |

还有一个隐性的：**插入顺序**。必须先插 `role` 再插 `user`，否则外键不成立。
脚本用目标库的拓扑顺序（`MetaData.sorted_tables`）自动排序。

---

## 四、实测结果

环境：Windows 11 + PostgreSQL 17.2（官方免安装二进制，端口 55432，
项目内 `data/pgdata`，不装系统服务、不依赖 Docker）。

```
① 启动 PostgreSQL …            started
② 准备数据库 …                 rag 新建
③ 执行 Alembic 迁移 …          upgrade → 0001
④ 建表后体检 …                 表 18/18   索引 42   外键 14   结论 通过
⑤ 迁移数据 …                   320 行；逐表一致：是
⑥ 迁移后体检 …                 结论 通过
⑦ 序列探针 …                   new_id=6（说明序列已推到 max(id)+1，不会冲突）
   时区探针 …                  timestamp with time zone
=== 总体：通过 ===
```

逐表行数（迁移后与迁移前完全一致）：

| 表 | 行数 | 表 | 行数 |
| --- | ---: | --- | ---: |
| user | 5 | message | 17 |
| role | 5 | ref_snapshot | 26 |
| knowledge_base | 5 | compliance_term | 22 |
| document | 23 | compliance_rule | 4 |
| doc_version | 23 | compliance_event | 114 |
| document_meta | 23 | sql_template | 4 |
| chunk | 29 | sql_exec_audit | 3 |
| session | 9 | feedback / badcase / eval_run | 1 / 1 / 14 |

**应用级烟测**（`DATABASE_URL` 指向 PG，用真实应用跑完整链路）：

| 检查 | 结果 |
| --- | --- |
| 管理员登录 | 200 |
| `system/db` | `backend=postgresql`、`alembic_revision=0001`、`timezone=UTC`、19 张表 |
| 文档列表 | 23 篇（跨库权限过滤正常） |
| 检索 | 5 条召回，37ms |
| SSE 问答 | `meta → delta × N → done`，消息与会话落 PG |
| 会话列表 | 11 个（含迁移过来的 8 个） |
| 应用连接时区 | `UTC`，读出的时间戳 `+00:00` |

**自动化测试**：`132 passed, 1 skipped`（新增 20 条，覆盖时区往返、PG 方言编译、
值转换、自检接口、迁移完整性）。SQLite 老路径全部回归通过，没有"改 A 坏 B"。

---

## 五、过程中发现并修掉的问题

1. **迁移脚本引用了应用模块**：`autogenerate` 生成了 `app.db.types.TZDateTime(...)`，
   但迁移文件本应自包含（换台机器跑 `alembic upgrade` 就 ImportError）→ 改成原生
   `sa.DateTime(timezone=True)`，DDL 完全等价。
2. **downgrade 表名写错**：以为审计表叫 `sql_audit`，实际是 `sql_exec_audit`。
   这个问题是在"升级→回滚→再升级"往返测试里暴露的——只测升级永远发现不了。
3. **naive/aware 混比**：改类型后 `badcases.py` 和 `monitor.py` 里的
   `datetime.utcnow()` 会和新类型冲突，同步改成 aware。
4. **SQLite 外键默认关闭**：本地永远测不出外键问题，生产一插就炸 → 加 PRAGMA。
5. **autogenerate 的 batch 块**：因为我们给 SQLite 开了 `render_as_batch`，
   生成的迁移把每条索引都包进 `batch_alter_table`。新建表后建索引用普通
   `create_index` 更干净，PG 上生成的 SQL 也更直接。
6. **JSON 双重编码**：迁移前列类型转换时就预判并处理，否则会出现"看着有数据、
   程序读出来是字符串"的诡异 bug。
7. **`pg_ctl` 启动导致命令行挂起**：postgres 守护进程继承了父进程的输出管道，
   调用方永远等不到 EOF → 启动时把输出重定向到 `DEVNULL`，服务端日志走 `-l` 文件。

---

## 六、怎么用

### 本地开发（默认，什么都不用改）

```bash
cd backend
uvicorn app.main:app --port 8000     # 仍是 SQLite，零依赖
```

### 本机验证 PostgreSQL（不装 Docker、不装系统服务）

```bash
cd backend
python scripts/local_pg.py up                 # 解压+initdb+启动+建库（只需一次）
python scripts/verify_pg.py                   # 建表→搬数据→体检→探针，一条命令跑完
python scripts/pg_smoke_app.py                # 用真实应用在 PG 上跑问答
```

### 切到 PG 运行应用

```bash
# .env
DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55432/rag
DB_AUTO_CREATE=false        # 生产：表结构只由迁移变更

alembic upgrade head        # 建表（幂等）
python scripts/migrate_sqlite_to_pg.py --target "$DATABASE_URL" --dry-run   # 先演练
python scripts/migrate_sqlite_to_pg.py --target "$DATABASE_URL"             # 正式搬
python scripts/check_db.py --url "$DATABASE_URL"                            # 体检
uvicorn app.main:app --port 8000
```

### 预发/生产（Docker）

```bash
docker compose -f deploy/docker-compose.yml --profile core up -d pg
alembic -x url=postgresql+psycopg://rag:密码@pg:5432/rag upgrade head
```

---

## 七、已知边界（进入 10-2 前要清楚）

1. **业务库（汇率/报价/关税）仍是 SQLite**：`trade.db` 与主库物理分离，走只读连接 +
   SQLite authorizer 做表级授权。切 PG 需要把"authorizer 授权"换成"只读角色 + 视图授权"，
   属于独立改造，不影响主链路，安排在部署步骤一起做。
2. **本机 PG 只用于验证**：`data/pgdata` 是临时实例（端口 55432、trust 认证），
   不是生产形态。生产用 Docker 或云数据库，开密码认证、开备份。
3. **没做读写分离 / 连接池中间件**：当前是应用内连接池，单实例够用；
   多副本部署时总连接数 = 副本数 × (pool_size + overflow)，需要按 PG 的
   `max_connections` 反算，10-4 压测时校准。
4. **备份与 PITR 未配置**：属于部署步骤（pg_dump 定时 + WAL 归档），本步只保证能迁。
5. **`alembic.ini` 的 `sqlalchemy.url` 故意留空**：连接串统一从 `.env` 读，
   避免"迁移跑到了 A 库、应用连的是 B 库"这种事故。

---

## 八、下一步（10-2）

Celery + Redis 异步流水线：上传立即返回、OCR/切分/嵌入/入库进队列，
外加任务进度查询页。要解决的是现在"上传一个大 PDF，接口卡住几分钟直到 OCR 跑完"的问题。
