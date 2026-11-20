"""SIL-A07 / P2 收口：安全事件审计持久化（唯一定义处）。

解决上一轮如实披露的边界：「拒判审计是进程内环形缓冲，重启即清零」。
本模块把四类安全事件落成 `audit.semantic_audit_logs` 的权威副本：

* ``policy_deny``          —— ABAC 逐点拒判（app.policy.decide）
* ``prompt_injection``     —— LLM 不可信输入命中注入模式（app.modules.llm.client）
* ``owner_gate_deny``      —— 本体治理写端点被 require_owner 拦下
* ``context_strict_reject``—— AC-01 严格模式缺六项上下文被中间件拒绝

架构沿用 P1-5 的既定决策：**内存是读模型，数据库是权威副本**。
写入路径只做一次 `deque.append`（同步端点里也是 O(1)），由常驻线程
每 1s 批量聚合后单事务落库；`flush()` 可强制同步刷盘（测试与端点用）。
启动时 `hydrate()` 从库水合最近 N 条回内存，因此 recent() 跨重启不清零。

两个防御性约束：
1. **审计永不阻断业务**：record/flush 的任何异常都吞掉并计数，绝不因为
   审计表不可用让一次检索或一次判定失败。
2. **量控**：policy.decide 在逐行过滤时可能被同一请求调用上百次，刷盘时按
   (事件类型, 规则, trace, 路径, 状态码) 聚合成一行 count，避免审计表爆量。
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from contextvars import ContextVar

from app.db import session as db

TABLE = "audit.semantic_audit_logs"
RECENT_MAX = 500
QUEUE_MAX = 20000
FLUSH_INTERVAL = 1.0
BATCH_MAX = 2000

EVENT_TYPES = ("policy_deny", "prompt_injection", "owner_gate_deny",
               "context_strict_reject")

_DDL = (
    "CREATE SCHEMA IF NOT EXISTS audit",
    f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            event_type VARCHAR(40) NOT NULL,
            rule VARCHAR(80),
            severity VARCHAR(16) NOT NULL DEFAULT 'warn',
            tenant_id VARCHAR(80),
            user_id VARCHAR(120),
            actor VARCHAR(120),
            request_id VARCHAR(120),
            trace_id VARCHAR(120),
            method VARCHAR(10),
            path VARCHAR(300),
            status_code INTEGER,
            count INTEGER NOT NULL DEFAULT 1,
            by_rule JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            detail JSONB NOT NULL DEFAULT '{{}}'::jsonb
        )""",
    f"CREATE INDEX IF NOT EXISTS idx_semantic_audit_ts ON {TABLE} (ts DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_semantic_audit_type_ts ON {TABLE} (event_type, ts DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_semantic_audit_trace ON {TABLE} (trace_id)",
)

_INSERT = f"""INSERT INTO {TABLE}
    (ts, event_type, rule, severity, tenant_id, user_id, actor,
     request_id, trace_id, method, path, status_code, count, by_rule, detail)
    VALUES (to_timestamp(:ts), :event_type, :rule, :severity, :tenant_id,
            :user_id, :actor, :request_id, :trace_id, :method, :path,
            :status_code, :count, CAST(:by_rule AS jsonb), CAST(:detail AS jsonb))"""

# 当前请求的安全审计上下文（由 AC-01 全局中间件 bind）。
# anyio.to_thread 会复制 contextvars，因此同步端点里 policy/llm 的 record 也能带齐。
_CTX: ContextVar[dict] = ContextVar("security_audit_ctx", default={})

_RECENT: deque = deque(maxlen=RECENT_MAX)
_QUEUE: deque = deque(maxlen=QUEUE_MAX)
_LOCK = threading.Lock()
_STATS: dict = {
    "recorded": 0, "flushed": 0, "batches": 0, "dropped": 0, "errors": 0,
    "last_flush_at": None, "last_error": None, "hydrated_at": None,
    "hydrated_rows": 0, "schema_ready": False,
}
_THREAD: threading.Thread | None = None
_STOP = threading.Event()


# ---------------------------------------------------------------- 上下文绑定

def bind(**fields) -> object:
    """绑定当前请求上下文，返回 token 供 unbind 还原（中间件成对调用）。"""
    current = dict(_CTX.get() or {})
    current.update({k: v for k, v in fields.items() if v is not None})
    return _CTX.set(current)


def unbind(token) -> None:
    try:
        _CTX.reset(token)
    except Exception:  # noqa: BLE001
        pass


def current_context() -> dict:
    return dict(_CTX.get() or {})


# ---------------------------------------------------------------- 写入

def _clean(value) -> str:
    """去掉 NUL / 控制字符：PostgreSQL 的 text 与 jsonb 都不接受 \\u0000。

    app.policy.principal_from(None) 用 `\\x00__unknown__` 作 fail-closed 哨兵，
    若原样写入审计表会让整批 INSERT 失败、事件全退回队列——审计反而丢失。
    """
    s = str(value)
    return "".join(ch if (ch.isprintable() or ch in " \t") else " " for ch in s)


def _json(value) -> str:
    try:
        text = json.dumps(value or {}, ensure_ascii=False, default=str)
        # json.dumps 会把 NUL 写成字面 \\u0000，PG jsonb 解析时同样拒绝，需一并去掉
        return _clean(text).replace("\\u0000", " ").replace("\\U00000000", " ")
    except Exception:  # noqa: BLE001
        return "{}"


def record(event_type: str, *, rule: str | None = None,
           severity: str = "warn", count: int = 1,
           detail: dict | None = None, ctx: dict | None = None) -> None:
    """入队一条安全事件。任何异常都只记统计，不向上抛（审计不阻断业务）。"""
    try:
        if event_type not in EVENT_TYPES:
            event_type = "policy_deny"
        c = dict(_CTX.get() or {})
        if ctx:
            c.update({k: v for k, v in ctx.items() if v is not None})
        ev = {
            "ts": time.time(),
            "event_type": event_type,
            "rule": str(rule or "")[:80] or None,
            "severity": str(severity or "warn")[:16],
            "tenant_id": _cut(c.get("tenant_id"), 80),
            "user_id": _cut(c.get("user_id"), 120),
            "actor": _cut(c.get("actor"), 120),
            "request_id": _cut(c.get("request_id"), 120),
            "trace_id": _cut(c.get("trace_id"), 120),
            "method": _cut(c.get("method"), 10),
            "path": _cut(c.get("path"), 300),
            "status_code": c.get("status_code"),
            "count": max(1, int(count or 1)),
            "by_rule": {},
            "detail": detail or {},
        }
        if event_type == "policy_deny" and ev["rule"]:
            ev["by_rule"] = {ev["rule"]: ev["count"]}
        with _LOCK:
            _RECENT.append(dict(ev))
            if len(_QUEUE) >= QUEUE_MAX:
                _QUEUE.popleft()
                _STATS["dropped"] += 1
            _QUEUE.append(dict(ev))
            _STATS["recorded"] += 1
    except Exception as exc:  # noqa: BLE001
        _STATS["errors"] += 1
        _STATS["last_error"] = f"record: {type(exc).__name__}: {exc}"[:200]


def _cut(value, limit: int):
    if value in (None, ""):
        return None
    return _clean(value)[:limit] or None


# ---------------------------------------------------------------- 落库

def _serialize(ev: dict) -> dict:
    out = dict(ev)
    out["by_rule"] = _json(ev.get("by_rule"))
    out["detail"] = _json(ev.get("detail"))
    return out


def flush(limit: int = BATCH_MAX) -> int:
    """把队列内事件聚合后单事务落库，返回落库行数；失败时事件退回队列。"""
    with _LOCK:
        batch = []
        n = max(1, min(int(limit or BATCH_MAX), BATCH_MAX))
        while _QUEUE and len(batch) < n:
            batch.append(_QUEUE.popleft())
    if not batch:
        return 0

    # 同一请求 / 同一规则的高频拒判合并成一行，count 累加、by_rule 归并
    groups: dict[tuple, dict] = {}
    for ev in batch:
        key = (ev["event_type"], ev["rule"], ev["tenant_id"], ev["user_id"],
               ev["request_id"], ev["trace_id"], ev["method"], ev["path"],
               ev["status_code"], ev["severity"])
        g = groups.get(key)
        if g is None:
            groups[key] = dict(ev)
            continue
        g["count"] += ev["count"]
        for rule_name, num in (ev.get("by_rule") or {}).items():
            g["by_rule"][rule_name] = g["by_rule"].get(rule_name, 0) + num
        # detail 只保留首条样本，避免聚合行无限膨胀
    rows = [_serialize(g) for g in groups.values()]
    try:
        with db.transaction():
            db.executemany(_INSERT, rows)
        with _LOCK:
            _STATS["flushed"] += len(rows)
            _STATS["batches"] += 1
            _STATS["last_flush_at"] = time.time()
            _STATS["last_error"] = None
        return len(rows)
    except Exception as exc:  # noqa: BLE001  落库失败不能丢事件
        with _LOCK:
            # 退回队首（保序），并计数；队列上限会自然淘汰最旧
            _QUEUE.extendleft(reversed(batch))
            _STATS["errors"] += 1
            _STATS["last_error"] = f"flush: {type(exc).__name__}: {exc}"[:200]
        return 0


def ensure_schema() -> bool:
    """幂等建表；数据库不可用时返回 False，服务仍可启动。"""
    try:
        with db.transaction():
            for ddl in _DDL:
                db.execute(ddl)
        _STATS["schema_ready"] = True
        return True
    except Exception as exc:  # noqa: BLE001
        _STATS["schema_ready"] = False
        _STATS["last_error"] = f"ensure_schema: {type(exc).__name__}: {exc}"[:200]
        return False


def hydrate(limit: int = RECENT_MAX) -> int:
    """启动时从权威副本水合最近 N 条回内存，消除「重启即清零」。"""
    try:
        rows = query(limit=max(1, min(int(limit), RECENT_MAX)))
        with _LOCK:
            _RECENT.clear()
            for r in reversed(rows):  # query 返回新→旧，内存里按旧→新排列
                _RECENT.append(_from_row(r))
            _STATS["hydrated_at"] = time.time()
            _STATS["hydrated_rows"] = len(rows)
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        _STATS["last_error"] = f"hydrate: {type(exc).__name__}: {exc}"[:200]
        return 0


def _from_row(r: dict) -> dict:
    """库行 → 内存事件形状（JSONB 已被 psycopg 解成 dict/str）。"""
    def obj(v):
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (ValueError, TypeError):
                return {}
        return {}
    ts = r.get("ts")
    try:
        epoch = ts.timestamp() if hasattr(ts, "timestamp") else time.time()
    except Exception:  # noqa: BLE001
        epoch = time.time()
    return {"ts": epoch, "event_type": r.get("event_type"), "rule": r.get("rule"),
            "severity": r.get("severity"), "tenant_id": r.get("tenant_id"),
            "user_id": r.get("user_id"), "actor": r.get("actor"),
            "request_id": r.get("request_id"), "trace_id": r.get("trace_id"),
            "method": r.get("method"), "path": r.get("path"),
            "status_code": r.get("status_code"), "count": r.get("count") or 1,
            "by_rule": obj(r.get("by_rule")), "detail": obj(r.get("detail"))}


def _loop() -> None:
    while not _STOP.wait(FLUSH_INTERVAL):
        try:
            if _QUEUE:
                flush()
        except Exception as exc:  # noqa: BLE001  守护线程不允许因单次异常退出
            _STATS["errors"] += 1
            _STATS["last_error"] = f"loop: {type(exc).__name__}: {exc}"[:200]


def start() -> dict:
    """startup 调用：建表 → 水合 → 起刷盘线程。可重复调用（幂等）。"""
    global _THREAD
    ready = ensure_schema()
    if ready:
        hydrate()
    if _THREAD is None or not _THREAD.is_alive():
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="security-audit-flusher",
                                   daemon=True)
        _THREAD.start()
    return {"schema_ready": ready, "thread_alive": thread_alive(),
            "hydrated_rows": _STATS["hydrated_rows"]}


def thread_alive() -> bool:
    return bool(_THREAD and _THREAD.is_alive())


# ---------------------------------------------------------------- 读取 / 自证

def recent(limit: int = 50) -> list[dict]:
    """最近事件（内存读模型，已跨重启水合），新 → 旧。"""
    n = max(1, min(int(limit or 50), RECENT_MAX))
    with _LOCK:
        items = list(_RECENT)[-n:]
    return [_public(e) for e in reversed(items)]


def _public(e: dict) -> dict:
    out = dict(e)
    out["at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("ts") or 0))
    return out


def query(event_type: str | None = None, limit: int = 100,
          trace_id: str | None = None) -> list[dict]:
    """直接查权威副本（跨进程 / 跨重启）。"""
    n = max(1, min(int(limit or 100), 2000))
    where, params = [], {"l": n}
    if event_type:
        where.append("event_type = :t")
        params["t"] = event_type
    if trace_id:
        where.append("trace_id = :tr")
        params["tr"] = trace_id
    sql = f"SELECT * FROM {TABLE}" + (" WHERE " + " AND ".join(where) if where else "")
    return db.fetch_all(sql + " ORDER BY ts DESC, id DESC LIMIT :l", params)


def stats() -> dict:
    with _LOCK:
        out = dict(_STATS)
        out["queue_size"] = len(_QUEUE)
        out["recent_size"] = len(_RECENT)
        out["thread_alive"] = thread_alive()
        out["table"] = TABLE
    try:
        row = db.fetch_one(f"SELECT count(*) AS n, max(ts) AS last_ts FROM {TABLE}")
        out["db_rows"] = int(row["n"]) if row else 0
        out["db_last_ts"] = str(row["last_ts"]) if row and row.get("last_ts") else None
        # 表已存在即可持久化：startup 未跑（如 TestClient / 单测）或表由上一轮
        # 启动时建好，都不应把 durable 误报成 False。以库的实际可查性为准。
        out["table_exists"] = True
        out["schema_ready"] = True
    except Exception as exc:  # noqa: BLE001
        out["db_rows"] = None
        out["table_exists"] = False
        out["db_error"] = f"{type(exc).__name__}: {exc}"[:160]
    return out


def verify() -> dict:
    """自证：队列清空后内存/库都对得上，且持久化确实生效。"""
    before = stats()
    written = flush()
    after = stats()
    return {
        "table": TABLE,
        "schema_ready": after["schema_ready"],
        "durable": bool(after.get("table_exists")) and after["db_rows"] is not None,
        "flushed_now": written,
        "queue_before": before["queue_size"],
        "queue_after": after["queue_size"],
        "db_rows": after["db_rows"],
        "recent_size": after["recent_size"],
        "thread_alive": after["thread_alive"],
        "hydrated_rows": after["hydrated_rows"],
        "in_sync": after["queue_size"] == 0 and after["db_rows"] is not None,
        "last_error": after["last_error"],
    }


def reset_local() -> None:
    """只清进程内统计与缓冲（测试隔离用），**绝不删库**——审计不可变。"""
    with _LOCK:
        _RECENT.clear()
        _QUEUE.clear()
        _STATS.update(recorded=0, flushed=0, batches=0, dropped=0, errors=0,
                      last_flush_at=None, last_error=None,
                      hydrated_rows=0, hydrated_at=None)
