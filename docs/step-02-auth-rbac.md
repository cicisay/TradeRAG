# 步骤 2 实施记录：数据库模型 + JWT 登录 + RBAC（M1）

日期：2026-11-09　对应里程碑：M1　对应 PRD：6.1 数据模型 / 5.6 FR-601~605 / 5.8.2 E6xxx

## 本步做了什么

### 1. 数据库模型（app/models/、app/db/session.py）
- `user` 表：username / password_hash（bcrypt 哈希）/ role_id / **biz_group（业务组，FR-603 隔离核心）** / status / created_at
- `role` 表：五种内置角色 code（admin/auditor/compliance/knowledge/user，FR-601）
- `knowledge_base` 表：name / biz_group / product_line / embedding_model / created_by（FR-111 分库骨架）
- SQLAlchemy 2.0 风格；`get_db` 依赖注入请求级会话；生产切 PostgreSQL 仅改 DATABASE_URL

### 2. JWT 认证（app/core/security.py）
- 密码 bcrypt 加盐哈希，不存明文；登录签发 JWT（HS256，8 小时有效）
- 载荷含身份四要素（id/username/role/biz_group），检索隔离直接使用
- 错误码对齐 PRD E6xxx：E6001 登录过期/凭证无效

### 3. RBAC 权限矩阵（app/core/permissions.py、app/core/deps.py）
- 8 个权限编码（chat:use、kb:read、kb:manage、compliance:manage、sql:manage、audit:read、debug:retrieval、admin:users）
- 单一事实来源 `ROLE_PERMISSIONS`，角色×权限全表一处维护
- `require_permission(perm)` 依赖工厂：401（未登录/过期）与 403（越权 E6003）分离
- 用户被禁用后旧令牌立即失效（每请求查库校验 status）

### 4. 接口与种子数据（app/api/auth.py、app/db/seed.py）
- POST /api/v1/auth/login、GET /api/v1/auth/me、GET /api/v1/admin/ping（RBAC 验证端点）
- 登录失败统一文案防用户名枚举；启动 lifespan 自动建表+幂等种子
- 默认管理员 admin / admin123（记录在案，改密功能后续步骤提供）

### 5. 测试（tests/test_auth.py，9 用例全过）
- 登录成功/密码错/未知用户同文案；/me 带令牌/无令牌/伪造令牌；RBAC 管理员放行/普通用户 403/业务组字段正确

## 自查发现并修复的 Bug（2 个）

1. **config.py 路径拼接错误**：sqlite URL 为绝对路径（sqlite:///E:/...）时目录创建逻辑拼出非法路径。已修复为标准 Path 解析并兼容 `:memory:`。
2. **路由前缀重复**：auth.router 自带 /api/v1/auth，挂载时又加 /api/v1 导致 404。已修复并加注释防复发。
3. 补装 httpx（TestClient 必需，requirements 已转正式依赖）。

## 验证结果

- pytest：**9 passed**（语法编译 COMPILE_OK）
- 真实服务冒烟：admin 登录 → /me 返回角色与 8 项权限 → admin/ping 200 → 无令牌 401 ✅

## 已知待办

- 修改密码/用户管理 CRUD 接口（M1 收尾或随前端步骤）
- document/chunk/ref_snapshot 等其余表 → M2 流水线步骤按需创建
- 生产 PostgreSQL + Alembic 迁移 → M6
