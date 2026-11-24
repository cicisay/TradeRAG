"""Milvus RBAC 初始化：创建最小权限业务账号并授权（10-4，幂等可重复执行）。

前置：Milvus 已开启 authorizationEnabled（compose 环境变量 COMMON_SECURITY_AUTHORIZATIONENABLED）。
账号与密码取 backend/.env 的 MILVUS_USER / MILVUS_PASSWORD。

用法（backend/）：.venv/Scripts/python scripts/milvus_rbac_setup.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymilvus import MilvusClient  # noqa: E402

from app.config import get_settings  # noqa: E402

ROLE = "traderag_app"

# 权限收敛到"读写向量 + 管自己的 collection"，不给用户管理/删除其他库的能力
GLOBAL_PRIVS = ["ListCollections", "DescribeDatabase"]
COLLECTION_PRIVS = [
    "CreateCollection", "DropCollection", "DescribeCollection",
    "CreateIndex", "DropIndex", "IndexDetail", "Load", "Release",
    "Insert", "Delete", "Search", "Query", "Flush", "GetStatistics", "GetFlushState",
]


def main() -> int:
    s = get_settings()
    root = MilvusClient(uri=s.milvus_uri, user="root", password="Milvus")

    users = list(root.list_users())
    if s.milvus_user in users:
        print(f"① 用户 {s.milvus_user} 已存在")
    else:
        root.create_user(user_name=s.milvus_user, password=s.milvus_password)
        print(f"① 用户 {s.milvus_user} 已创建")

    roles = [r["role"] if isinstance(r, dict) else r for r in root.list_roles()]
    if ROLE in roles:
        print(f"② 角色 {ROLE} 已存在")
    else:
        root.create_role(role_name=ROLE)
        print(f"② 角色 {ROLE} 已创建")

    granted = 0
    for priv in GLOBAL_PRIVS:
        try:
            root.grant_privilege(role_name=ROLE, object_type="Global", privilege=priv, object_name="*")
            granted += 1
        except Exception:
            pass
    for priv in COLLECTION_PRIVS:
        try:
            root.grant_privilege(role_name=ROLE, object_type="Collection", privilege=priv, object_name="*")
            granted += 1
        except Exception:
            pass
    print(f"③ 授权完成：{granted} 条（重复执行会跳过已有的）")

    try:
        root.grant_role(user_name=s.milvus_user, role_name=ROLE)
        print(f"④ 已把角色 {ROLE} 授予 {s.milvus_user}")
    except Exception as exc:
        print(f"④ 角色绑定：{exc}（若已绑定可忽略）")

    # 自检：用业务账号连接并读一次 collection 统计
    app = MilvusClient(uri=s.milvus_uri, user=s.milvus_user, password=s.milvus_password)
    cols = app.list_collections()
    print(f"⑤ 业务账号连接成功，可见 collection：{cols}")
    for c in cols:
        print(f"   {c} 行数 = {app.get_collection_stats(c)['row_count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
