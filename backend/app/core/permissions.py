"""RBAC 权限矩阵（对应 PRD FR-601 角色、FR-602 权限矩阵、FR-604 双人复核前提）。

给新手的说明：
- "权限"用 统一小写编码 表示（如 kb:manage = 管理知识库/文档）。
- "角色 → 权限集合"的映射只维护在这一张表里（单一事实来源）：
  以后前端菜单显隐、后端接口鉴权都查这里，改一处全局生效。
- FR-604（词库/SQL 模板双人复核）等流程控制属于 M4 步骤，这里先把"谁能进管理口"管住。

权限编码规划（与 PRD 第 9 章 API 对齐）：
- chat:use        普通问答
- kb:read         浏览知识库/文档/分片
- kb:manage       上传/删除文档、版本归档（知识管理员）
- compliance:manage  合规词库/规则的提交与复核（合规管理员）
- sql:manage      SQL 模板注册与复核（合规管理员，M4）
- audit:read      审计事件/合规事件查看（审计员、合规管理员）
- debug:retrieval 召回测试台/批量评测（FR-707：仅管理员/审计员）
- admin:users     用户与角色管理（系统管理员）
"""

# 系统内置角色（PRD FR-601 五种）
ROLE_ADMIN = "admin"              # 系统管理员
ROLE_AUDITOR = "auditor"          # 安全审计员
ROLE_COMPLIANCE = "compliance"    # 合规管理员
ROLE_KNOWLEDGE = "knowledge"      # 知识管理员
ROLE_USER = "user"                # 普通用户

ROLE_NAMES: dict[str, str] = {
    ROLE_ADMIN: "系统管理员",
    ROLE_AUDITOR: "安全审计员",
    ROLE_COMPLIANCE: "合规管理员",
    ROLE_KNOWLEDGE: "知识管理员",
    ROLE_USER: "普通用户",
}

# 权限矩阵：角色 → 允许的权限集合
ROLE_PERMISSIONS: dict[str, set[str]] = {
    ROLE_ADMIN: {
        "chat:use", "kb:read", "kb:manage", "compliance:manage",
        "sql:manage", "audit:read", "debug:retrieval", "admin:users",
    },
    ROLE_AUDITOR: {
        "chat:use", "kb:read", "audit:read", "debug:retrieval",
    },
    ROLE_COMPLIANCE: {
        "chat:use", "kb:read", "compliance:manage", "sql:manage", "audit:read",
    },
    ROLE_KNOWLEDGE: {
        "chat:use", "kb:read", "kb:manage",
    },
    ROLE_USER: {
        "chat:use", "kb:read",
    },
}


def permissions_of_role(role_code: str) -> set[str]:
    """取某角色的权限集合；未知角色返回空集（默认无权限，安全兜底）。"""
    return ROLE_PERMISSIONS.get(role_code, set())


# 登录入口归属：前端登录页有「用户登录 / 管理员登录」两个 tab，
# 后端必须校验账号与入口是否匹配——否则后台账号能从普通用户入口登进来，
# 入口区分就成了纯装饰（2026-11-09 修复）。
USER_PORTAL_ROLES: set[str] = {ROLE_USER}
ADMIN_PORTAL_ROLES: set[str] = {ROLE_ADMIN, ROLE_AUDITOR, ROLE_COMPLIANCE, ROLE_KNOWLEDGE}


def portal_of_role(role_code: str) -> str:
    """角色归属的登录入口：'admin'（后台角色）或 'user'（普通用户）。"""
    return "admin" if role_code in ADMIN_PORTAL_ROLES else "user"
