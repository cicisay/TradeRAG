"""多线程分片下载（临时工具）：单连接被限速时，用 HTTP Range 并行拉取再拼接。

用法：python scripts/_fastdl.py <url> <输出文件> [线程数]
"""
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

URL = sys.argv[1]
OUT = Path(sys.argv[2])
N = int(sys.argv[3]) if len(sys.argv) > 3 else 16

req = urllib.request.Request(URL, method="HEAD")
with urllib.request.urlopen(req, timeout=30) as r:
    SIZE = int(r.headers["Content-Length"])
print(f"size={SIZE/1024/1024:.1f} MB, threads={N}", flush=True)

CHUNK = SIZE // N
parts = OUT.parent / f"_{OUT.name}.parts"
parts.mkdir(exist_ok=True)


def fetch(i: int) -> int:
    start = i * CHUNK
    end = SIZE - 1 if i == N - 1 else (i + 1) * CHUNK - 1
    path = parts / f"{i:03d}"
    if path.exists() and path.stat().st_size == end - start + 1:
        return path.stat().st_size
    req = urllib.request.Request(URL, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    path.write_bytes(data)
    return len(data)


t0 = time.time()
done = 0
with ThreadPoolExecutor(N) as ex:
    futs = {ex.submit(fetch, i): i for i in range(N)}
    for f in as_completed(futs):
        done += f.result()
        el = time.time() - t0
        print(f"  {done/1024/1024:6.1f}/{SIZE/1024/1024:.1f} MB  "
              f"{done/1024/el:6.0f} KB/s  {el:5.1f}s", flush=True)

with OUT.open("wb") as fh:
    for i in range(N):
        fh.write((parts / f"{i:03d}").read_bytes())
print(f"done {OUT} {OUT.stat().st_size} bytes in {time.time()-t0:.1f}s", flush=True)
