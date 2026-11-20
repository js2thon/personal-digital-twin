"""个人数字分身 · 本地部署健康自检（只读，不修改任何服务状态）。

用法：
    python check_health.py
退出码：
    0 = 全部通过；1 = 至少一项失败。
检查项（仅本机 127.0.0.1，不联网、不对外）：
    1. PostgreSQL 5434 端口监听
    2. API 8101 端口监听
    3. GET /healthz 返回 {"status":"ok"}
    4. GET /console/ 返回 HTML 控制台
    5. GET /api/v1/ontology/semantic/schema 返回真实 schema（证明 API+DB 通）
    6. GET /api/v1/ontology/v2/schema 状态为 published（证明本体已冻结发布）
"""
from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request

PG_PORT = 5434
API_PORT = 8101
BASE = f"http://127.0.0.1:{API_PORT}"
TIMEOUT = 6

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


def port_open(port: int) -> tuple[bool, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.settimeout(0.6)
        ok = sk.connect_ex(("127.0.0.1", port)) == 0
    return ok, ("监听中" if ok else "未监听")


def http_json(path: str) -> tuple[int, object]:
    """返回 (http_status, parsed_json_or_None)。"""
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def http_text(path: str) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def main() -> int:
    results: list[tuple[bool, str, str]] = []

    def add(ok: bool, name: str, detail: str) -> None:
        results.append((ok, name, detail))

    ok, detail = port_open(PG_PORT)
    add(ok, f"PostgreSQL 端口 {PG_PORT}", detail)

    ok, detail = port_open(API_PORT)
    add(ok, f"API 端口 {API_PORT}", detail)

    code, body = http_json("/healthz")
    add(code == 200 and isinstance(body, dict) and body.get("status") == "ok",
        "健康检查 /healthz", f"HTTP {code} · {body}")

    code, text = http_text("/console/")
    add(code == 200 and "<!DOCTYPE HTML>" in text.upper(),
        "控制台 /console/", f"HTTP {code} · HTML {len(text)} 字节")

    code, body = http_json("/api/v1/ontology/semantic/schema")
    ent = body.get("entity_types") if isinstance(body, dict) else None
    add(code == 200 and isinstance(ent, list) and len(ent) > 0,
        "语义层 schema（API+DB 连通）",
        f"HTTP {code} · 实体类型 {len(ent) if isinstance(ent, list) else '—'} 个")

    code, body = http_json("/api/v1/ontology/v2/schema")
    status = body.get("schema", {}).get("status") if isinstance(body, dict) else None
    add(code == 200 and status == "published",
        "本体发布态 v2", f"HTTP {code} · status={status}")

    print("=" * 56)
    print(" 个人数字分身 · 本地部署健康自检")
    print(f" 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}   目标：{BASE}")
    print("=" * 56)
    for ok, name, detail in results:
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {name:<28} {detail}")
    passed = sum(1 for r in results if r[0])
    print("-" * 56)
    print(f" 结果：{passed}/{len(results)} 项通过")
    if passed == len(results):
        print(" 服务健康。浏览器打开：  http://127.0.0.1:8101/")
    else:
        print(" 存在失败项，参见《本地部署-安装启停与排障手册》排障章节。")
    print("=" * 56)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
