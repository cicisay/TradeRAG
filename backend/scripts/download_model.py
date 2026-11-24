"""模型下载工具：从 ModelScope 拉取 bge-m3（国内比 hf-mirror 快很多）。

用法（backend/ 目录）：
    .venv/Scripts/python scripts/download_model.py                # 下载 bge-m3 到 models/bge-m3
    .venv/Scripts/python scripts/download_model.py --model BAAI/bge-reranker-base

为什么不用 huggingface_hub：hf-mirror 实测约 0.4MB/s，2.2GB 要一个多小时；
ModelScope 支持 Range 分片，多线程并行能到几 MB/s。
"""
import argparse
import concurrent.futures as futures
import os
import sys
import time

import httpx

MS_API = "https://www.modelscope.cn/api/v1/models/{model}/repo"
MS_FILE = MS_API + "?Revision=master&FilePath={path}"

# sentence-transformers 加载所需文件（bge-m3 结构）
REQUIRED = [
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "sentence_bert_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "1_Pooling/config.json",
]
WEIGHT = "pytorch_model.bin"


def download_small(model: str, files: list[str], target: str) -> None:
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for path in files:
            dest = os.path.join(target, path)
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                print(f"  跳过已存在 {path}")
                continue
            r = client.get(MS_FILE.format(model=model, path=path))
            if r.status_code != 200:
                print(f"  ✗ {path} -> HTTP {r.status_code}（该文件可能不存在，跳过）")
                continue
            with open(dest, "wb") as f:
                f.write(r.content)
            print(f"  ✓ {path} ({len(r.content) / 1024:.0f} KB)")


def _download_chunk(model: str, path: str, start: int, end: int, out: str, idx: int) -> int:
    headers = {"Range": f"bytes={start}-{end}"}
    written = 0
    with httpx.Client(timeout=300, follow_redirects=True) as client:
        with client.stream("GET", MS_FILE.format(model=model, path=path), headers=headers) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_bytes(1024 * 512):
                    f.write(chunk)
                    written += len(chunk)
    return written


def download_weight(model: str, target: str, workers: int = 4) -> str:
    dest = os.path.join(target, WEIGHT)
    if os.path.exists(dest) and os.path.getsize(dest) > 100 * 1024 * 1024:
        print(f"  跳过：{WEIGHT} 已存在（{os.path.getsize(dest) / 1e9:.2f} GB）")
        return dest

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        head = client.head(MS_FILE.format(model=model, path=WEIGHT))
        total = int(head.headers.get("content-length", 0))
    if total <= 0:
        # 服务器不支持 HEAD 时按已知大小兜底
        total = 2271 * 1024 * 1024
    print(f"  权重大小 {total / 1e9:.2f} GB，分 {workers} 片并行下载…")

    step = total // workers + 1
    parts = []
    started = time.time()
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = []
        for i in range(workers):
            start = i * step
            end = min(total - 1, start + step - 1)
            part = f"{dest}.part{i}"
            parts.append(part)
            jobs.append(pool.submit(_download_chunk, model, WEIGHT, start, end, part, i))
        sizes = []
        try:
            for job, part in zip(jobs, parts):
                job.result()
                sizes.append(os.path.getsize(part))
                got = sum(sizes)
                print(f"  分片完成 {len(sizes)}/{workers}  累计 {got / 1e9:.2f} GB  "
                      f"{got / 1e6 / max(time.time() - started, 1):.1f} MB/s")
        except Exception as exc:
            print("  分片下载失败：", exc)
            raise

    with open(dest, "wb") as out:
        for part in parts:
            with open(part, "rb") as f:
                while True:
                    buf = f.read(1024 * 1024)
                    if not buf:
                        break
                    out.write(buf)
            os.remove(part)
    print(f"  ✓ {WEIGHT} 合并完成 {os.path.getsize(dest) / 1e9:.2f} GB")
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="BAAI/bge-m3")
    parser.add_argument("--target", default=None)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    target = args.target or os.path.join("models", args.model.split("/")[-1])
    os.makedirs(target, exist_ok=True)
    print(f"下载 {args.model} → {target}")
    download_small(args.model, REQUIRED, target)
    download_weight(args.model, target, args.workers)
    print("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
