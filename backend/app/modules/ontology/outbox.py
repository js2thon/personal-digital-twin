"""SIL-EC-01～EC-05：Outbox 事件账本与投影分发。

## 为什么是「升级」而不是「替换」

项目原有的实时链路是：`knowledge.facts` 触发器 → `pg_notify` → 常驻 LISTEN
线程 → `resync_fact()`，实测 0.13s 级完成边变化，且能覆盖「绕过 API 直接改库」
这种 Async Outbox 覆盖不到的场景（见规范 EC-05 判定）。因此本模块**不动**那条
低延迟通道，只补它缺的三件事：

1. **可审计 / 可重放的账本**：`ingest.outbox_events` 此前是死表（0 行，仅
   `sources.py` 写过两类与投影无关的记录）；
2. **同事务原子性**：事件由触发器在事实写入的**同一事务**内 INSERT，不存在
   「事实成功、事件丢失」的中间态，也不需要应用层逐处加钩子（16+ 处写入点必漏）；
3. **多目标分发**：`resync_fact()` 收口到 `ProjectionWorker` 接口，图与向量
   各自实现，可单独降级、单独重建。

## 三条硬约束（对应 EC-01 / EC-03）

- `payload` 只放标识与必要上下文，业务规则由 Worker 读**已提交**数据后重算，
  避免事件体与事实双写不一致；
- 消费是**幂等**的：同一事件重放 100 次，派生边数量不变（`resync_fact` 按三元组比对）；
- 失败不丢：`attempts` 递增到上限置 `dead`，`last_error` 留痕，可人工 `replay`。

## 与 `processed` 旧列的兼容

`ingest.outbox_events.processed` 是 V1.2 就有的列，`sources.py` 依赖它。本模块
用触发器让 `processed` 永远等于 `status = 'processed'`，既不改调用方，也不允许
两份状态漂移。
"""
from __future__ import annotations

import logging
import threading
import time

from app.db import session as db

log = logging.getLogger("twin.outbox")

# ---------------------------------------------------------------- 枚举（EC-02）
EVENT_TYPES = (
    "fact.created", "fact.updated", "fact.withdrawn",
    "entity.merged", "ontology.published", "document.changed",
    "embedding.model_upgraded", "permission.changed", "source.synced",
)
FACT_EVENTS = ("fact.created", "fact.updated", "fact.withdrawn")

PENDING, PROCESSING, PROCESSED, FAILED, DEAD = (
    "pending", "processing", "processed", "failed", "dead")
STATUSES = (PENDING, PROCESSING, PROCESSED, FAILED, DEAD)

# 消费参数：批大小、轮询间隔、最大重试次数（均可 env 覆盖）
MAX_ATTEMPTS = 5
BATCH = 100
INTERVAL = 1.0

_BATCH_SQL = """
WITH picked AS (
    SELECT id FROM ingest.outbox_events
     WHERE status IN ('pending', 'failed')
       AND attempts < :max_attempts
     ORDER BY occurred_at
     LIMIT :n
     FOR UPDATE SKIP LOCKED
)
UPDATE ingest.outbox_events o
   SET status = 'processing', attempts = o.attempts + 1
  FROM picked p
 WHERE o.id = p.id
RETURNING o.id::text AS id, o.event_type, o.aggregate_id::text AS aggregate_id,
          o.aggregate_type, o.tenant_id, o.trace_id, o.payload, o.attempts,
          o.occurred_at
"""

_FINISH_SQL = """
UPDATE ingest.outbox_events
   SET status = CASE WHEN {ok} THEN 'processed'
                     WHEN attempts >= :max_attempts THEN 'dead'
                     ELSE 'failed' END,
       processed_at = CASE WHEN {ok} THEN NOW() ELSE NULL END,
       last_error = CASE WHEN {ok} THEN NULL ELSE :err END
 WHERE id = CAST(:id AS uuid)
"""

# `processed` ↔ `status` 单向同步：插入时若调用方只给了 processed=true
# （sources.py 的历史写法），等价于 status='processed'。
_SYNC_FN = """
CREATE OR REPLACE FUNCTION ingest.fn_outbox_sync_processed() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status = 'pending' AND NEW.processed IS TRUE THEN
            NEW.status := 'processed';
            NEW.processed_at := COALESCE(NEW.processed_at, NOW());
            NEW.attempts := GREATEST(NEW.attempts, 1);
        ELSE
            NEW.processed := (NEW.status = 'processed');
        END IF;
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        NEW.processed := (NEW.status = 'processed');
    END IF;
    RETURN NEW;
END;
$fn$
"""

_ENUM_SQL = ", ".join(f"'{t}'" for t in EVENT_TYPES)
_STATUS_SQL = ", ".join(f"'{s}'" for s in STATUSES)


def _schema_sql() -> list[tuple[str, str]]:
    """(名称, DDL)。全部幂等；单条失败只记录不抛，避免拖垮启动。"""
    return [
        ("add_columns",
         "ALTER TABLE ingest.outbox_events "
         " ADD COLUMN IF NOT EXISTS aggregate_type VARCHAR(50) NOT NULL DEFAULT 'Fact',"
         " ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',"
         " ADD COLUMN IF NOT EXISTS ontology_version VARCHAR(50),"
         " ADD COLUMN IF NOT EXISTS schema_version VARCHAR(10) NOT NULL DEFAULT '1',"
         " ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64),"
         " ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
         " ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ,"
         " ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,"
         " ADD COLUMN IF NOT EXISTS last_error TEXT,"
         " ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'pending'"),
        ("event_type_enum",
         "DO $do$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
         "WHERE conname = 'outbox_event_type_enum' "
         "AND conrelid = 'ingest.outbox_events'::regclass) THEN "
         f"ALTER TABLE ingest.outbox_events ADD CONSTRAINT outbox_event_type_enum "
         f"CHECK (event_type IN ({_ENUM_SQL})); END IF; END $do$;"),
        ("status_enum",
         "DO $do$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
         "WHERE conname = 'outbox_status_enum' "
         "AND conrelid = 'ingest.outbox_events'::regclass) THEN "
         f"ALTER TABLE ingest.outbox_events ADD CONSTRAINT outbox_status_enum "
         f"CHECK (status IN ({_STATUS_SQL})); END IF; END $do$;"),
        ("attempts_nonnegative",
         "DO $do$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint "
         "WHERE conname = 'outbox_attempts_nonnegative' "
         "AND conrelid = 'ingest.outbox_events'::regclass) THEN "
         "ALTER TABLE ingest.outbox_events ADD CONSTRAINT outbox_attempts_nonnegative "
         "CHECK (attempts >= 0); END IF; END $do$;"),
        ("idx_pending",
         "CREATE INDEX IF NOT EXISTS idx_outbox_pending "
         "ON ingest.outbox_events (occurred_at) "
         "WHERE status IN ('pending', 'failed')"),
        ("idx_aggregate",
         "CREATE INDEX IF NOT EXISTS idx_outbox_aggregate "
         "ON ingest.outbox_events (aggregate_id, occurred_at DESC)"),
        ("idx_type",
         "CREATE INDEX IF NOT EXISTS idx_outbox_type "
         "ON ingest.outbox_events (event_type, occurred_at DESC)"),
        ("sync_fn", _SYNC_FN),
        ("sync_trigger",
         "DROP TRIGGER IF EXISTS trg_outbox_sync_processed ON ingest.outbox_events; "
         "CREATE TRIGGER trg_outbox_sync_processed "
         "BEFORE INSERT OR UPDATE ON ingest.outbox_events "
         "FOR EACH ROW EXECUTE FUNCTION ingest.fn_outbox_sync_processed()"),
    ]


_ensured = False
_ensure_lock = threading.Lock()
_schema_report: dict = {"applied": [], "failed": []}


def ensure_schema(force: bool = False) -> dict:
    """幂等升级账本结构。返回每条 DDL 的结果，便于端点自证。"""
    global _ensured
    if _ensured and not force:
        return _schema_report
    with _ensure_lock:
        if _ensured and not force:
            return _schema_report
        applied, failed = [], []
        for name, ddl in _schema_sql():
            try:
                db.execute(ddl)
                applied.append(name)
            except Exception as exc:            # noqa: BLE001  单条失败不阻断
                failed.append({"name": name, "error": f"{type(exc).__name__}: {exc}"[:200]})
                log.warning("outbox ddl %s failed: %s", name, exc)
        _schema_report.update({"applied": applied, "failed": failed})
        _ensured = True
    return _schema_report


# ---------------------------------------------------------------- 写入（触发器侧）
# 事实事件由 `knowledge.facts` 触发器直接 INSERT，保证与事实写入同事务。
# 这里只提供「非事实类」事件的写入助手，供业务侧显式记账。
def record(event_type: str, aggregate_id: str, payload: dict, *,
           aggregate_type: str = "Fact", tenant_id: str = "default",
           ontology_version: str | None = None, trace_id: str | None = None,
           status: str = PENDING) -> str | None:
    """显式落一条事件（调用方需已在事务内，或接受独立事务）。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type: {event_type}")
    return db.insert_returning_id("ingest.outbox_events", {
        "event_type": event_type,
        "aggregate_id": aggregate_id,
        "aggregate_type": aggregate_type,
        "tenant_id": tenant_id,
        "ontology_version": ontology_version,
        "trace_id": trace_id,
        "payload": db_json(payload),
        "status": status,
    })


def db_json(v) -> str:
    import json

    return json.dumps(v, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- 查询与统计
def stats() -> dict:
    """账本一屏：状态分布 + 积压延迟 + 近 24h 事件类型分布。"""
    by_status = {r["status"]: int(r["n"]) for r in db.fetch_all(
        "SELECT status, count(*) AS n FROM ingest.outbox_events GROUP BY 1")}
    oldest = db.fetch_one(
        "SELECT EXTRACT(EPOCH FROM (NOW() - min(occurred_at))) AS lag, "
        "       count(*) AS n FROM ingest.outbox_events "
        " WHERE status IN ('pending', 'failed')")
    by_type = [{"event_type": r["event_type"], "n": int(r["n"])}
               for r in db.fetch_all(
                   "SELECT event_type, count(*) AS n FROM ingest.outbox_events "
                   "WHERE occurred_at > NOW() - INTERVAL '24 hours' "
                   "GROUP BY 1 ORDER BY 2 DESC")]
    backlog = {s: by_status.get(s, 0) for s in STATUSES}
    pending_lag = float((oldest or {}).get("lag") or 0)
    return {
        "total": sum(by_status.values()),
        "by_status": backlog,
        "backlog": int(backlog[PENDING] + backlog[FAILED] + backlog[PROCESSING]),
        "dead": int(backlog[DEAD]),
        "pending_lag_seconds": round(pending_lag, 3),
        "oldest_pending": int((oldest or {}).get("n") or 0),
        "by_type_24h": by_type,
        "schema": _schema_report,
        "dispatcher": dispatcher_stats(),
    }


def recent(limit: int = 20, status: str | None = None) -> list[dict]:
    sql = ("SELECT id::text AS id, event_type, aggregate_type, aggregate_id::text AS aggregate_id, "
           "       tenant_id, trace_id, status, attempts, last_error, "
           "       payload, occurred_at, processed_at "
           "FROM ingest.outbox_events ")
    params: dict = {"n": max(1, min(limit, 200))}
    if status:
        sql += "WHERE status = :s "
        params["s"] = status
    sql += "ORDER BY occurred_at DESC LIMIT :n"
    return db.fetch_all(sql, params)


# ---------------------------------------------------------------- 消费
def claim(limit: int = BATCH, max_attempts: int = MAX_ATTEMPTS) -> list[dict]:
    """取一批待处理事件并标记为 processing（SKIP LOCKED 支持多消费者）。"""
    with db.transaction():
        return db.fetch_all(_BATCH_SQL, {"n": max(1, min(limit, 500)),
                                         "max_attempts": max_attempts})


def finish(event_id: str, ok: bool, error: str | None = None,
           attempts: int = 0, max_attempts: int = MAX_ATTEMPTS) -> str:
    """收口一条事件，返回最终状态。"""
    db.execute(_FINISH_SQL.format(ok="TRUE" if ok else "FALSE"),
               {"id": event_id, "err": (error or "")[:2000] or None,
                "max_attempts": int(max_attempts)})
    if ok:
        return PROCESSED
    return DEAD if int(attempts) >= int(max_attempts) else FAILED


def drain(limit: int = BATCH, max_attempts: int | None = None) -> dict:
    """消费一批：批内同一聚合只投影一次（Worker 读的是当前已提交数据，
    同一 fact 的多条事件处理一次等价，避免批量撤销时重复重算）。"""
    from app.modules.ontology import projection_worker as workers

    mx = int(max_attempts or MAX_ATTEMPTS)
    events = claim(limit, max_attempts=mx)
    if not events:
        return {"claimed": 0, "processed": 0, "failed": 0, "coalesced": 0,
                "workers": workers.registry()}

    seen: dict[str, str] = {}
    ok_n = fail_n = coalesced = 0
    errors: list[str] = []
    for ev in events:
        key = f"{ev['aggregate_type']}:{ev['aggregate_id']}"
        if key in seen:
            finish(ev["id"], True)
            coalesced += 1
            continue
        seen[key] = ev["id"]
        try:
            res = workers.dispatch(ev)
        except Exception as exc:                # noqa: BLE001  单条失败不拖垮整批
            state = finish(ev["id"], False, f"{type(exc).__name__}: {exc}",
                           attempts=ev["attempts"], max_attempts=mx)
            fail_n += 1
            errors.append(f"{ev['event_type']}({ev['aggregate_id']}) -> {state}: {exc}")
            continue
        if res.get("ok"):
            finish(ev["id"], True)
            ok_n += 1
        else:
            state = finish(ev["id"], False, res.get("error") or "worker_failed",
                           attempts=ev["attempts"], max_attempts=mx)
            fail_n += 1
            errors.append(f"{ev['event_type']}({ev['aggregate_id']}) -> {state}: "
                          f"{res.get('error')}")
    if errors:
        log.warning("outbox drain: %d failed, first=%s", len(errors), errors[0][:200])
    return {"claimed": len(events), "processed": ok_n, "failed": fail_n,
            "coalesced": coalesced, "errors": errors[:10]}


def replay(statuses: tuple[str, ...] = (FAILED, DEAD),
           limit: int = 200, dry_run: bool = True) -> dict:
    """把失败 / 死信事件重置为 pending，等 Dispatcher 重新投递。

    只重置计数，不改 payload —— 重放的含义是「同一条事件再试一次」，
    不是「伪造一条新事件」。
    """
    entries = ",".join(f"'{s}'" for s in statuses)
    picked = db.fetch_one(
        "SELECT count(*) AS n FROM ("
        f"  SELECT 1 FROM ingest.outbox_events WHERE status IN ({entries}) "
        "   ORDER BY occurred_at LIMIT :l) t", {"l": int(limit)})
    n = int((picked or {}).get("n") or 0)
    if dry_run or not n:
        return {"dry_run": dry_run, "statuses": list(statuses), "would_replay": n,
                "replayed": 0}
    row = db.fetch_one(
        f"WITH picked AS (SELECT id FROM ingest.outbox_events WHERE status IN ({entries}) "
        "ORDER BY occurred_at LIMIT :l), "
        "u AS (UPDATE ingest.outbox_events o SET status = 'pending', attempts = 0, "
        "       last_error = NULL FROM picked p WHERE o.id = p.id RETURNING 1) "
        "SELECT count(*) AS n FROM u", {"l": int(limit)})
    replayed = int((row or {}).get("n") or 0)
    log.info("outbox replay: %d events -> pending", replayed)
    return {"dry_run": False, "statuses": list(statuses), "would_replay": n,
            "replayed": replayed}


# ---------------------------------------------------------------- Dispatcher 线程
_stop = threading.Event()
_thread: threading.Thread | None = None
_start_lock = threading.Lock()
_stats = {"runs": 0, "claimed": 0, "processed": 0, "failed": 0,
          "coalesced": 0, "last_run_at": 0.0, "last_error": None, "enabled": True}


def dispatcher_stats() -> dict:
    return {**_stats, "interval_seconds": INTERVAL, "batch": BATCH,
            "max_attempts": MAX_ATTEMPTS,
            "thread_alive": bool(_thread and _thread.is_alive())}


def _loop() -> None:
    while not _stop.is_set():
        if _stats["enabled"]:
            try:
                r = drain()
                if r["claimed"]:
                    _stats.update({
                        "runs": _stats["runs"] + 1,
                        "claimed": _stats["claimed"] + r["claimed"],
                        "processed": _stats["processed"] + r["processed"],
                        "failed": _stats["failed"] + r["failed"],
                        "coalesced": _stats["coalesced"] + r["coalesced"],
                        "last_run_at": time.time(), "last_error": None})
                    log.info("outbox drain: claimed=%d ok=%d failed=%d coalesced=%d",
                             r["claimed"], r["processed"], r["failed"], r["coalesced"])
            except Exception as exc:            # noqa: BLE001  数据库抖动不退出
                _stats["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("outbox dispatcher error: %s", exc)
        _stop.wait(INTERVAL)


def start() -> bool:
    """startup 调用：升级结构 + 起常驻 Dispatcher（幂等）。"""
    global _thread
    with _start_lock:
        if _thread and _thread.is_alive():
            return True
        try:
            ensure_schema()
        except Exception as exc:                # noqa: BLE001
            _stats["last_error"] = f"ensure_schema: {exc}"[:300]
            log.warning("outbox schema deferred: %s", exc)
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="outbox-dispatcher", daemon=True)
        _thread.start()
        log.info("outbox dispatcher started (interval=%.1fs, batch=%d)", INTERVAL, BATCH)
    return True


def stop() -> None:
    _stop.set()
