"""本地 Redis 管理（M6 步骤10-2，免 Docker）。

为什么需要这个脚本：Redis 官方不提供 Windows 版，Docker 又要额外装。
这里用 tporadowski 维护的 5.0.14 移植版（生产请用 Linux 官方 7.x），
只用于本机开发与冒烟验证，不能让业务逻辑依赖 5.0 的老特性。

用法：
    python scripts/local_redis.py up       # 下载（首次）+ 启动，端口 56379
    python scripts/local_redis.py status    # 看是否可连、版本、内存
    python scripts/local_redis.py down      # 关闭

端口刻意用 56379 而不是 6379：避免和你以后装的正经 Redis/Docker 冲突，
也让"这个库是临时验证用的"一眼可辨。
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
BIN = DATA / "_redisbin"
ZIP = DATA / "_redis.zip"
LOG = DATA / "redis.log"
PORT = int(os.getenv("LOCAL_REDIS_PORT", "56379"))

# GitHub 直连在部分网络下不通，用加速镜像；可用环境变量覆盖
# 用 Redis 8 的 Windows 构建（msys2 编译）：
# 5.0 的老移植版跑不了 Celery/kombu 5.7 的 Lua 脚本（ERR syntax error），
# 而本机没有 Docker，所以选这个接近生产版本的构建。
MIRROR = os.getenv(
    "REDIS_MIRROR",
    "https://gh-proxy.com/https://github.com/redis-windows/redis-windows/releases/download/8.10.3/Redis-8.10.3-Windows-x64-msys2.zip",
)


def exe(name: str) -> Path:
    return BIN / (name + (".exe" if os.name == "nt" else ""))


def _ensure_binaries() -> bool:
    if exe("redis-server").exists():
        return True
    print(f"未找到 Redis 二进制，开始下载：{MIRROR}")
    DATA.mkdir(parents=True, exist_ok=True)
    rc = subprocess.run(
        ["curl", "-L", "--retry", "3", "-o", str(ZIP), MIRROR],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode
    if rc != 0 or not ZIP.exists():
        print("下载失败。请手动下载 Redis for Windows 并解压到 data/_redisbin/")
        return False
    with zipfile.ZipFile(ZIP) as z:
        z.extractall(BIN)
    ZIP.unlink(missing_ok=True)
    # 有些发布包解压后多一层目录，把真正的可执行文件提到 _redisbin 根下
    if not exe("redis-server").exists():
        for candidate in BIN.rglob("redis-server.exe" if os.name == "nt" else "redis-server"):
            for item in candidate.parent.iterdir():
                target = BIN / item.name
                if not target.exists():
                    shutil.move(str(item), str(target))
            break
    return exe("redis-server").exists()


def ping() -> bool:
    import redis

    try:
        # protocol=2：Redis 5 不认识 redis-py 8 默认发的 HELLO 3
        client = redis.Redis(host="127.0.0.1", port=PORT, socket_connect_timeout=0.5,
                             socket_timeout=0.5, protocol=2)
        ok = bool(client.ping())
        client.close()
        return ok
    except Exception:
        return False


def cmd_up() -> int:
    if ping():
        print(f"Redis 已在运行：127.0.0.1:{PORT}")
        return 0
    if not _ensure_binaries():
        return 2
    DATA.mkdir(parents=True, exist_ok=True)
    log = open(LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(exe("redis-server")), "--port", str(PORT), "--bind", "127.0.0.1",
         "--save", "", "--appendonly", "no", "--maxmemory", "256mb",
         "--maxmemory-policy", "allkeys-lru"],
        cwd=BIN, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    for _ in range(40):
        if ping():
            print(f"Redis 已启动：redis://127.0.0.1:{PORT}/0（pid={proc.pid}，日志 {LOG}）")
            return 0
        time.sleep(0.25)
    print(f"Redis 启动超时，见日志 {LOG}")
    return 1


def cmd_down() -> int:
    if not ping():
        print("Redis 未运行")
        return 0
    try:
        import redis

        client = redis.Redis(host="127.0.0.1", port=PORT, socket_timeout=2, protocol=2)
        client.shutdown(nosave=True)
        print("Redis 已关闭")
        return 0
    except Exception as exc:
        print(f"关闭失败（可能已退出）：{exc}")
        return 0


def cmd_status() -> int:
    if not ping():
        print(f"Redis 未运行（端口 {PORT}）")
        return 1
    import redis

    client = redis.Redis(host="127.0.0.1", port=PORT, protocol=2)
    info = client.info()
    print(f"可连接：redis://127.0.0.1:{PORT}/0")
    print(f"版本 {info.get('redis_version')} · 内存 {info.get('used_memory_human')} · "
          f"连接 {info.get('connected_clients')} · 运行 {info.get('uptime_in_seconds')}s")
    print(f"ingest 队列长度：{client.llen('ingest')}")
    client.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 Redis（Windows 免安装）")
    parser.add_argument("cmd", choices=["up", "down", "status"])
    args = parser.parse_args()
    return {"up": cmd_up, "down": cmd_down, "status": cmd_status}[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
