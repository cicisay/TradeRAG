"""重建索引脚本：换嵌入模型 / 内存向量库重启后，把已解析文档重新切分+嵌入+入库。

什么时候需要跑：
- 内存向量库（VECTOR_STORE=memory）每次重启都会清空 → 重启后检索不到东西，跑一次即可。
- 换了嵌入模型（hash → bge-m3）→ 旧向量维度/语义空间不同，必须重建
  （PRD 17.4 反锁定规则：换模型要新建 Collection，不能混用旧向量）。

用法（在 backend/ 目录）：
    .venv/Scripts/python scripts/reindex.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import SessionLocal, init_db  # noqa: E402
from app.models.document import STATUS_PARSED, Document  # noqa: E402
from app.services.embedding import get_embedder  # noqa: E402
from app.services.pipeline import run_indexing  # noqa: E402


def main() -> int:
    init_db()
    db = SessionLocal()
    total_chunks = 0
    failed = 0
    try:
        docs = db.query(Document).filter(Document.status == STATUS_PARSED).all()
        print(f"待重建文档：{len(docs)} 篇")
        for doc in docs:
            try:
                stats = run_indexing(doc.id, db)
                total_chunks += stats["chunks"]
                print(f"  ✓ doc_id={doc.id} {doc.doc_name} → {stats['chunks']} 块")
            except Exception as exc:
                failed += 1
                db.rollback()
                print(f"  ✗ doc_id={doc.id} {doc.doc_name} 失败：{exc}")
        print(f"\n完成：{len(docs) - failed} 篇成功，{failed} 篇失败，共 {total_chunks} 块")
        print(f"当前嵌入器：{get_embedder().name}")
    finally:
        db.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
