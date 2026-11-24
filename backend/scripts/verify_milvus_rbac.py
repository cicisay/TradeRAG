"""10-4 Milvus RBAC 验证（容器化部署安全项）。

验证三件事：
1. 匿名连接被拒绝（authorizationEnabled 生效）；
2. 出厂账号 root/Milvus 可用（当前 .env 采用的应用凭据）；
3. collection 数据可读（业务未受影响）。

用法（backend/）：.venv/Scripts/python scripts/verify_milvus_rbac.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymilvus import MilvusClient  # noqa: E402

URI = "http://127.0.0.1:19530"
COL = "chunks_bge_m3_1024"


def main() -> int:
    ok = True

    # 1) 匿名必须被拒
    try:
        MilvusClient(uri=URI).list_collections()
        print("❌ 匿名连接成功——RBAC 未生效！")
        ok = False
    except Exception as exc:
        print(f"① 匿名连接被拒 ✅（{type(exc).__name__}: {str(exc)[:80]}…）")

    # 2) 出厂账号可用
    try:
        c = MilvusClient(uri=URI, user="root", password="Milvus")
        print("② root/Milvus 连接 ✅")
    except Exception as exc:
        print(f"❌ root 连接失败：{exc}")
        return 1

    # 3) 业务数据可读
    try:
        rows = c.get_collection_stats(COL)["row_count"]
        print(f"③ {COL} 行数 = {rows} ✅")
    except Exception as exc:
        print(f"❌ 业务 collection 读取失败：{exc}")
        ok = False

    print("\nRBAC 验证：", "✅ PASS" if ok else "❌ FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
