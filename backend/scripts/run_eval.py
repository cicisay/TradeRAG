"""评测跑分 CLI（对应 PRD FR-705 / FR-706 的"每周回归、发版前跑分"）。

用法：
    python scripts/run_eval.py --list
    python scripts/run_eval.py --dataset compliance
    python scripts/run_eval.py --dataset retrieval_zh --top-k 5
    python scripts/run_eval.py --dataset all --limit 10        # 快速冒烟
    python scripts/run_eval.py --dataset retrieval_crosslingual --user demo_sales

退出码：合规集出现 redline_missed（该拦未拦）时返回 1，可直接接 CI 作门禁。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.db.session import SessionLocal
from app.models.user import User
from app.services.evaluation import list_datasets, run_eval


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="all", help="评测集名称，或 all")
    parser.add_argument("--list", action="store_true", help="只列出评测集")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟用）")
    parser.add_argument("--user", default="demo_sales", help="以哪个用户身份跑（影响权限过滤）")
    parser.add_argument("--no-rerank", action="store_true", help="关闭重排，作为重排前基线")
    parser.add_argument("--rerank-top-n", type=int, default=10, help="参与重排的候选数（小一点更快）")
    parser.add_argument("--no-save", action="store_true", help="不写 eval_run 表与报告文件")
    args = parser.parse_args()

    if args.list:
        for item in list_datasets():
            print(f"{item['name']:28} {item['cases']:4} 条  {item.get('kind')}  {item.get('languages')}")
        return 0

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == args.user).first()
        if user is None:
            print(f"用户不存在：{args.user}")
            return 2
        names = [d["name"] for d in list_datasets()] if args.dataset == "all" else [args.dataset]
        if not names:
            print("没有可用评测集")
            return 2

        # 内存向量库在独立进程里是空的：跑评测前先重建，否则"向量路"必然 0 召回。
        settings = get_settings()
        if settings.vector_store == "memory":
            from app.services.pipeline import reindex_all
            from app.services.vector_store import get_vector_store

            store = get_vector_store()
            if store.count(settings.milvus_collection) == 0:
                print("内存向量库为空，先重建索引（需要加载嵌入模型，约数十秒）…")
                print("  " + str(reindex_all(db)))

        redline = False
        for name in names:
            print(f"\n=== {name} ===")
            report = run_eval(
                db, user, name, top_k=args.top_k, limit=args.limit,
                rerank=not args.no_rerank, rerank_top_n=args.rerank_top_n,
                save=not args.no_save,
            )
            if report["kind"] == "retrieval":
                for channel in ("vector", "bm25", "hybrid"):
                    s = report["summary"][channel]
                    print(f"  {channel:7} Recall@{report['top_k']}={s['recall']:.3f}  MRR={s['mrr']:.3f}  nDCG={s['ndcg']:.3f}")
                for dimension in ("lang", "category"):
                    if f"by_{dimension}" in report["summary"]:
                        print(f"  by_{dimension}: " + ", ".join(
                            f"{k}={v['recall']:.2f}" for k, v in report["summary"][f"by_{dimension}"].items()
                        ))
            else:
                s = report["summary"]
                print(f"  accuracy={s['accuracy']:.3f}  block_recall={s['block_recall']}  "
                      f"block_precision={s['block_precision']}  notice_acc={s['notice_accuracy']:.3f}")
                if s["redline_missed"]:
                    redline = True
                    print(f"  !! 红线漏拦：{s['redline_missed']}")
                if s["false_blocks"]:
                    print(f"  !! 误拦截：{s['false_blocks']}")
            print(f"  cases={report['cases']}  duration={report['duration_ms']}ms  run_id={report.get('run_id')}")
        return 1 if redline else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
