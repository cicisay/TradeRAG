"""本地 PostgreSQL 管理（M6 开发/验收辅助，不用于生产）。

为什么有这个东西：本机没有 Docker，也不想为了验证一次迁移去装一整套服务。
这里用官方的"免安装二进制包"（EDB binaries zip）在项目目录里跑一个临时实例，
端口 55432，数据目录 data/pgdata，只监听 127.0.0.1，随用随停，不污染系统。

生产环境请用 Docker / 云数据库：
    docker compose -f deploy/docker-compose.yml --profile core up -d pg
本脚本只解决"手上没有 PG，但必须验证 PG 兼容性"的问题。

用法：
    python scripts/local_pg.py unzip     # 解压 data/_pgbin.zip → data/_pgbin/pgsql
    python scripts/local_pg.py init      # 初始化数据目录（只需一次）
    python scripts/local_pg.py start     # 启动（端口 55432）
    python scripts/local_pg.py createdb  # 创建 rag 库
    python scripts/local_pg.py status
    python scripts/local_pg.py stop
    python scripts/local_pg.py url       # 打印可直接用的连接串
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
ZIP = BACKEND_DIR / "data" / "_pgbin.zip"
ROOT = BACKEND_DIR / "data" / "_pgbin" / "pgsql"
DATA = BACKEND_DIR / "data" / "pgdata"
LOG = BACKEND_DIR / "data" / "pg.log"
PORT = 55432
DB = "rag"


def exe(name: str) -> str:
    p = ROOT / "bin" / (name + (".exe" if sys.platform == "win32" else ""))
    if not p.exists():
        raise SystemExit(f"找不到 {p}，请先执行：python scripts/local_pg.py unzip")
    return str(p)


def run(args: list[str], check: bool = True) -> int:
    print("$", " ".join(args))
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    if out.strip():
        print(out.strip()[:1500])
    if check and proc.returncode != 0:
        raise SystemExit(f"命令失败（{proc.returncode}）")
    return proc.returncode


def cmd_unzip() -> None:
    if ROOT.exists():
        print("已解压：", ROOT)
        return
    if not ZIP.exists():
        raise SystemExit(f"缺少 {ZIP}")
    print("解压中（约 300MB，需 1–2 分钟）…")
    with zipfile.ZipFile(ZIP) as zf:
        zf.extractall(ROOT.parent)
    print("解压完成：", ROOT)


def cmd_init() -> None:
    if (DATA / "PG_VERSION").exists():
        print("数据目录已初始化：", DATA)
        return
    run([exe("initdb"), "-D", str(DATA), "-U", "postgres", "-A", "trust",
         "-E", "UTF8", "--locale=C"])


def cmd_start() -> None:
    # 不捕获输出：postgres 守护进程会继承管道句柄，导致调用方永远等不到 EOF
    rc = subprocess.run(
        [exe("pg_ctl"), "-D", str(DATA), "-l", str(LOG),
         "-o", f"-p {PORT} -c listen_addresses=127.0.0.1", "-w", "start"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    ).returncode
    print("pg_ctl 退出码", rc, "（日志见 data/pg.log）")


def cmd_stop() -> None:
    run([exe("pg_ctl"), "-D", str(DATA), "-m", "fast", "stop"], check=False)


def cmd_status() -> None:
    run([exe("pg_ctl"), "-D", str(DATA), "status"], check=False)


def cmd_createdb() -> None:
    rc = run([exe("createdb"), "-h", "127.0.0.1", "-p", str(PORT), "-U", "postgres", DB], check=False)
    if rc != 0:
        print("（若提示已存在可忽略）")


def cmd_url() -> None:
    print(url())


def url() -> str:
    return f"postgresql+psycopg://postgres@127.0.0.1:{PORT}/{DB}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["unzip", "init", "start", "stop", "status", "createdb", "url", "up"])
    args = ap.parse_args()
    if args.action == "up":
        cmd_unzip()
        cmd_init()
        cmd_start()
        cmd_createdb()
        print("\n连接串：", url())
        return
    {"unzip": cmd_unzip, "init": cmd_init, "start": cmd_start, "stop": cmd_stop,
     "status": cmd_status, "createdb": cmd_createdb, "url": cmd_url}[args.action]()


if __name__ == "__main__":
    main()
