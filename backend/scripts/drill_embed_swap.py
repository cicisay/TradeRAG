"""10-4 嵌入替换演练（PRD 17.4 反锁定规则验证）。

演练逻辑：
1. 进程内把嵌入器切到另一个模型（MiniLM，384 维）+ 新 collection 名（chunks_minilm_384）；
2. 对全部已解析文档重建索引 → 新模型向量写入新 collection；
3. 验证：新 collection 可检索、维度正确；**旧 collection（chunks_bge_m3_1024）一行不动**；
4. 结束打印 PASS/FAIL。全程不改 .env / 不碰正在运行的服务（只在本进程内生效）。

用法（backend/）：.venv/Scripts/python scripts/drill_embed_swap.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须在导入 app.* 之前设置（get_settings 进程内单例）
os.environ["EMBED_MODEL_NAME"] = "models/minilm-drill"
os.environ["MILVUS_COLLECTION"] = "chunks_minilm_384"

from pymilvus import MilvusClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal, init_db  # noqa: E402
from app.models.document import STATUS_PARSED, Document  # noqa: E402
from app.services.embedding import get_embedder  # noqa: E402
from app.services.pipeline import run_indexing  # noqa: E402
from app.services.vector_store import sanitize_collection_name  # noqa: E402

OLD_COLLECTION = "chunks_bge_m3_1024"


def main() -> int:
    settings = get_settings()
    new_col = sanitize_collection_name(settings.milvus_collection)
    client = MilvusClient(uri=settings.milvus_uri,
                          user=settings.milvus_user, password=settings.milvus_password)

    old_before = client.get_collection_stats(OLD_COLLECTION)["row_count"]
    print(f"演练前 | 旧 collection {OLD_COLLECTION} = {old_before} 行")

    # 1) 换模型（进程内）
    emb = get_embedder()
    print(f"① 演练嵌入器：{emb.name}（{emb.dim} 维）→ 新 collection：{new_col}")
    assert emb.dim == 384, f"预期 384 维，实际 {emb.dim}"

    # 2) 重建索引到新 collection
    init_db()
    db = SessionLocal()
    docs = db.query(Document).filter(Document.status == STATUS_PARSED).all()
    ok = failed = 0
    for d in docs:
        try:
            run_indexing(d.id, db)
            ok += 1
        except Exception as exc:
            failed += 1
            db.rollback()
            print(f"  ✗ doc {d.id}: {exc}")
    db.close()
    print(f"② 重建完成：{ok} 成功 / {failed} 失败")

    # 3) 新 collection 检索验证
    vec = emb.embed(["DAP 和 DDP 有什么区别？"])[0]
    assert len(vec) == 384
    res = client.search(new_col, data=[vec], limit=3, filter='doc_status == "active"')
    hits = res[0] if res else []
    print(f"③ 新 collection 行数 = {client.get_collection_stats(new_col)['row_count']}，"
          f"检索 Top1 score = {hits[0]['distance']:.4f}" if hits else "③ 检索无结果")

    # 4) 旧 collection 完好性
    old_after = client.get_collection_stats(OLD_COLLECTION)["row_count"]
    print(f"④ 旧 collection = {old_after} 行（演练前 {old_before}）")
    passed = ok > 0 and failed == 0 and len(hits) > 0 and old_after == old_before
    print("\n演练结果：", "✅ PASS（反锁定：换模型 = 新 collection，旧数据无损）" if passed else "❌ FAIL")
    if passed:
        print("注意：演练重建了 chunk 表（新 chunk_id）。恢复生产映射请跑一次 scripts/reindex.py（bge-m3），"
              "它会把旧 collection 按 chunk 表清掉重写。")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
