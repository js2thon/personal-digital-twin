"""Personal Digital Twin - 静默后台服务启动器。

由 start-silent.bat 拉起（也可直接 python start_hidden.py）。
职责：
  1. 立刻隐藏自身控制台窗口（无黑框弹出）；
  2. 拉起内嵌 PostgreSQL 5434（若未运行）；
  3. 守护 API 8101：uvicorn 退出后 10 秒自动重启；
  4. 日志写入 .runtime/ 下：pg-stderr.log、uvicorn.out.log、uvicorn.err.log。

停止：运行 stop-services.bat，或结束 python / postgres 进程。
"""
from __future__ import annotations

import ctypes
import os
import pathlib
import socket
import subprocess
import sys
import time

SW_HIDE = 0
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

ROOT = pathlib.Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
BACKEND = ROOT / "backend"

PG_PORT = 5434
API_PORT = 8101


def hide_console() -> None:
    """隐藏当前进程所属的控制台窗口（启动瞬间即生效）。"""
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
    except Exception:
        pass


def log(name: str, text: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(RUNTIME / name, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"[{stamp}] {text}\n")


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.settimeout(0.4)
        return sk.connect_ex(("127.0.0.1", port)) == 0


def load_env() -> None:
    """把 .env 注入当前进程环境（与 start-services.bat 行为一致）。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def start_postgres() -> bool:
    if port_open(PG_PORT):
        return True
    exe = RUNTIME / "pgsql" / "bin" / "postgres.exe"
    data = RUNTIME / "pgdata"
    if not exe.exists() or not data.exists():
        log("uvicorn.log", f"postgres binaries missing: {exe}")
        return False
    RUNTIME.mkdir(parents=True, exist_ok=True)
    out = open(RUNTIME / "pg-stdout.log", "ab")
    err = open(RUNTIME / "pg-stderr.log", "ab")
    log("uvicorn.log", "starting postgres on 5434")
    subprocess.Popen(
        [str(exe), "-D", str(data), "-p", str(PG_PORT), "-h", "127.0.0.1"],
        stdout=out, stderr=err, creationflags=CREATE_NO_WINDOW,
    )
    for _ in range(60):
        if port_open(PG_PORT):
            log("uvicorn.log", "postgres is up")
            return True
        time.sleep(2)
    log("uvicorn.log", "postgres failed to start within 120s")
    return False


def has_uvicorn(exe: str) -> bool:
    """探测解释器能否真正 import uvicorn。

    必须同时校验 stdout 里有哨兵字符串：pythonw.exe 没有标准输出，探测会「静默失败」，
    只看 returncode 会把没装 uvicorn 的解释器误判为可用（曾导致 8101 每 10s 崩一次）。
    """
    try:
        proc = subprocess.run(
            [exe, "-c", "import uvicorn, fastapi; print('PDT_OK')"],
            capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return False
    return proc.returncode == 0 and b"PDT_OK" in (proc.stdout or b"")


def find_python() -> str:
    """挑一个真正装了 uvicorn 的解释器：优先项目 .venv，其次当前解释器同目录 python.exe，
    再退到系统安装的 3.12/3.11（WorkBuddy 内置 3.13 没有 uvicorn，PATH 通常指向它）。"""
    candidates: list[str] = []
    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        candidates.append(str(venv))
    exe = pathlib.Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        sibling = exe.with_name("python.exe")
        if sibling.exists():
            candidates.append(str(sibling))
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        for ver in ("Python312", "Python311"):
            cand = pathlib.Path(local) / "Programs" / "Python" / ver / "python.exe"
            if cand.exists():
                candidates.append(str(cand))
    candidates.append(str(exe))
    if exe.name.lower() == "pythonw.exe":
        candidates.append(str(exe))  # 最后手段：pythonw 也能跑，只是日志少

    for cand in candidates:
        if has_uvicorn(cand):
            if cand != str(exe):
                log("uvicorn.log", f"interpreter picked: {cand}")
            return cand
        log("uvicorn.log", f"interpreter rejected (no uvicorn): {cand}")
    log("uvicorn.log", "WARNING: no interpreter with uvicorn found, fallback to sys.executable")
    return sys.executable


def main() -> None:
    hide_console()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    os.environ["CELERY_TASK_ALWAYS_EAGER"] = "1"
    load_env()

    start_postgres()
    python_exe = find_python()
    log("uvicorn.log", f"supervisor using python: {python_exe}")

    while True:
        if not port_open(API_PORT):
            log("uvicorn.log", "starting uvicorn on 8101")
            out = open(RUNTIME / "uvicorn.out.log", "ab")
            err = open(RUNTIME / "uvicorn.err.log", "ab")
            try:
                proc = subprocess.Popen(
                    [python_exe, "-m", "uvicorn", "app.main:app",
                     "--host", "127.0.0.1", "--port", str(API_PORT),
                     # M7：uvicorn 默认会自行采信 X-Forwarded-For 并改写 client.host，
                     # 那样应用层的「默认不信任代理」会被架空。统一由 auth.py 解释代理头。
                     "--no-proxy-headers"],
                    cwd=str(BACKEND), stdout=out, stderr=err,
                    creationflags=CREATE_NO_WINDOW,
                )
                proc.wait()
                log("uvicorn.log", f"uvicorn exited with code {proc.returncode}")
            except Exception as exc:  # noqa: BLE001
                log("uvicorn.log", f"uvicorn start failed: {exc}")
            time.sleep(10)
        else:
            time.sleep(10)


if __name__ == "__main__":
    main()
