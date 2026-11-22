"""本机一键初始化：内嵌 PostgreSQL 数据目录 + 数据库 + 初始 DDL + Python 依赖。

由 bootstrap-local.bat 调用（使用项目 .venv 的 python）。幂等：已存在的步骤自动跳过。
日志：.runtime/bootstrap.log
"""
from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
RT = ROOT / ".runtime"
PGBIN = RT / "pgsql" / "bin"
PGDATA = RT / "pgdata"
PORT = 5434
DB = "personal_twin"
USER = "twin"
LOG = RT / "bootstrap.log"
NOWIN = 0x08000000


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def port_open(p: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", p)) == 0


def run(cmd: list[str], **kw) -> int:
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if r.stdout.strip():
        log(r.stdout.strip()[-2000:])
    if r.stderr.strip():
        log(r.stderr.strip()[-2000:])
    return r.returncode


def step_deps() -> None:
    need = []
    for mod, pkg in (("pandas", "pandas"), ("yaml", "pyyaml"), ("psycopg", "psycopg[binary]"),
                     ("pytest", "pytest"), ("openpyxl", "openpyxl"), ("multipart", "python-multipart")):
        try:
            __import__(mod)
        except ImportError:
            need.append(pkg)
    if need:
        log(f"安装依赖：{need}")
        run([sys.executable, "-m", "pip", "install", "-q", *need])
    else:
        log("依赖已齐")


def step_initdb() -> None:
    if (PGDATA / "PG_VERSION").exists():
        log("pgdata 已存在，跳过 initdb")
        return
    PGDATA.mkdir(parents=True, exist_ok=True)
    rc = run([str(PGBIN / "initdb.exe"), "-D", str(PGDATA), "-U", USER, "-A", "trust",
              "-E", "UTF8", "--locale=C"])
    if rc != 0:
        raise SystemExit("initdb 失败，见 .runtime/bootstrap.log")


def step_start_pg() -> None:
    if port_open(PORT):
        log(f"PostgreSQL 已在 {PORT} 运行")
        return
    rc = run([str(PGBIN / "pg_ctl.exe"), "-D", str(PGDATA), "-o", f"-p {PORT} -h 127.0.0.1",
              "-l", str(RT / "pg-stderr.log"), "-w", "-t", "300", "start"], creationflags=NOWIN)
    if rc != 0 or not port_open(PORT):
        raise SystemExit("PostgreSQL 启动失败，见 .runtime/pg-stderr.log")


def step_db_and_ddl() -> None:
    import psycopg
    with psycopg.connect(f"host=127.0.0.1 port={PORT} user={USER} dbname=postgres", autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB,)).fetchone():
            c.execute(f'CREATE DATABASE "{DB}" OWNER "{USER}" ENCODING \'UTF8\'')
            log(f"已创建数据库 {DB}")
        else:
            log(f"数据库 {DB} 已存在")
    with psycopg.connect(f"host=127.0.0.1 port={PORT} user={USER} dbname={DB}", autocommit=True) as c:
        has_vector = bool(c.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name='vector'").fetchone())
        already = c.execute("SELECT to_regclass('ontology.entities')").fetchone()[0]
        if already:
            log("初始表已存在，跳过 DDL")
            return
        for f in sorted((ROOT / "db" / "init").glob("*.sql")):
            sql = f.read_text(encoding="utf-8")
            if not has_vector:
                sql = sql.replace("CREATE EXTENSION IF NOT EXISTS vector;",
                                  "-- vector 扩展不可用（便携版 PG），跳过")
            try:
                c.execute(sql)
                log(f"已执行 {f.name}")
            except Exception as exc:  # noqa: BLE001
                log(f"执行 {f.name} 出错：{exc}")
                raise


def step_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    text = env.read_text(encoding="utf-8")
    want = f"DATABASE_URL=postgresql+psycopg://{USER}@127.0.0.1:{PORT}/{DB}"
    lines, changed = [], False
    for ln in text.splitlines():
        if ln.startswith("DATABASE_URL=") and ln.strip() != want:
            (RT / ".env.bak").write_text(text, encoding="utf-8")
            lines.append(want)
            changed = True
            log(f".env DATABASE_URL 改为本机内嵌库（原文件备份 .runtime/.env.bak）：{ln} → {want}")
        else:
            lines.append(ln)
    if changed:
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    RT.mkdir(exist_ok=True)
    log("==== bootstrap start ====")
    step_deps()
    step_initdb()
    step_start_pg()
    step_db_and_ddl()
    step_env()
    log("==== bootstrap OK ====")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        log(f"FAILED: {e}")
        raise
    except Exception as e:  # noqa: BLE001
        log(f"FAILED: {type(e).__name__}: {e}")
        raise SystemExit(1)
