"""事实变更 → 本体投影边的实时同步（事件驱动）。

链路：`knowledge.facts` 上的 AFTER 触发器**只发一个 fact_id 信号**（pg_notify），
PostgreSQL 在事务提交时才投递；本模块的常驻线程 LISTEN 后按当前**已提交**
的数据重算该事实撑起来的投影边。所有投影判断仍留在 Python 侧，触发器里
不含任何业务规则。

为什么不是轮询：轮询最快也只是准实时，且页面不开就不跑。
为什么不是应用层逐处加钩子：事实写入散落在 16+ 处 UPDATE / DELETE，
直接改库表也覆盖不到，逐处补必然漏。

为什么要启动补偿：服务停机期间发出的 NOTIFY 不会被留存，启动时
`catchup_relations()` 按当前事实底座重算一次投影边，补上这段空窗。
"""
from __future__ import annotations

import logging
import re
import select
import threading
import time
from queue import Empty, Queue

import psycopg

from app.config import settings
from app.modules.ontology import sync as graph_sync

log = logging.getLogger("twin.fact_events")

CHANNEL = "twin_fact_changed"
TRIGGER = "trg_twin_fact_changed"
MERGE_WINDOW = 0.08          # 同批事务（如批量撤销）合并成一个处理窗口
IDLE_SELECT = 0.5            # 空闲时 select 的最长等待
RETRY_WAIT = 3.0             # 断线重连间隔
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
CATCHUP_MIN_INTERVAL = 600


class _SkipCatchup(Exception):
    pass

_stop = threading.Event()
_thread: threading.Thread | None = None
_start_lock = threading.Lock()
_stats = {"connected": False, "batches": 0, "events": 0,
          "processed": 0, "failed": 0, "last_batch": 0, "last_at": 0.0,
          "catchup": None, "listener_error": None}


def _dsn() -> str:
    # SQLAlchemy 的 postgresql+psycopg:// 不能直接给 psycopg3 裸连接用
    return settings.database_url.replace("+psycopg", "", 1)


# 触发器函数用 $fn$ 而非 $$，避免与上层 SQL 解析冲突。
# 只在「会影响投影结果」的字段真的变了时发信号：trust_tier / confidence /
# source_ref 这类更新不触发重算，减少无谓写。
#
# v1.4（P1-1）：同一事务内**同时**落一条 Outbox 事件（SIL-EC-01 硬约束）：
# - 触发器里写账本，等于覆盖所有写入路径（含绕过 API 直接改库），且与事实
#   写入原子一致——不会出现「事实已提交、事件丢失」；
# - payload 只放标识与最小上下文，投影规则留在 Python 侧读已提交数据重算，
#   避免事件体与事实双写不一致；
# - pg_notify 保留原样，低延迟通道不受影响。
_DDL_FUNC = """
CREATE OR REPLACE FUNCTION knowledge.fn_twin_fact_changed() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
    fid uuid;
    meaningful boolean := true;
    ev_type text;
    tid text;
    oversion text;
    body jsonb;
BEGIN
    IF TG_OP = 'DELETE' THEN
        fid := OLD.id;
    ELSE
        fid := NEW.id;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        meaningful := (
               OLD.status      IS DISTINCT FROM NEW.status
            OR OLD.subject_id  IS DISTINCT FROM NEW.subject_id
            OR OLD.predicate   IS DISTINCT FROM NEW.predicate
            OR OLD.object_value IS DISTINCT FROM NEW.object_value
            OR OLD.valid_from  IS DISTINCT FROM NEW.valid_from
        );
    END IF;

    IF meaningful THEN
        IF TG_OP = 'DELETE' THEN
            ev_type := 'fact.withdrawn';
            tid := COALESCE(OLD.tenant_id, 'default');
            oversion := OLD.ontology_version;
            body := jsonb_build_object(
                'fact_id', OLD.id, 'op', 'DELETE',
                'subject_id', OLD.subject_id, 'predicate', OLD.predicate,
                'status', OLD.status);
        ELSE
            IF TG_OP = 'INSERT' THEN
                ev_type := 'fact.created';
            ELSIF NEW.status = 'withdrawn' THEN
                ev_type := 'fact.withdrawn';
            ELSE
                ev_type := 'fact.updated';
            END IF;
            tid := COALESCE(NEW.tenant_id, 'default');
            oversion := NEW.ontology_version;
            body := jsonb_build_object(
                'fact_id', NEW.id, 'op', TG_OP,
                'subject_id', NEW.subject_id, 'predicate', NEW.predicate,
                'status', NEW.status);
        END IF;

        -- 同事务落账本：失败会让事实写入一起回滚，这是有意的（宁可不写，
        -- 也不写出一笔没有事件的账）。
        INSERT INTO ingest.outbox_events
            (event_type, aggregate_type, aggregate_id, tenant_id,
             ontology_version, payload, occurred_at)
        VALUES (ev_type, 'Fact', fid, tid, oversion, body, NOW());

        PERFORM pg_notify('twin_fact_changed', fid::text);
    END IF;

    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END;
$fn$
"""

_DDL_TRIGGERS = [
    f"DROP TRIGGER IF EXISTS {TRIGGER} ON knowledge.facts",
    f"CREATE TRIGGER {TRIGGER} AFTER INSERT OR UPDATE OR DELETE "
    f"ON knowledge.facts FOR EACH ROW "
    f"EXECUTE FUNCTION knowledge.fn_twin_fact_changed()",
]


def ensure_trigger() -> bool:
    """幂等建立触发器（startup 调、每次重连也复查一次，存在就跳过）。"""
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL_FUNC)
            cur.execute(
                "SELECT 1 FROM pg_trigger WHERE tgname = %s "
                "AND tgrelid = 'knowledge.facts'::regclass AND NOT tgisinternal",
                (TRIGGER,))
            if not cur.fetchone():
                for stmt in _DDL_TRIGGERS:
                    cur.execute(stmt)
    return True


def stats() -> dict:
    """给诊断端点用：监听线程是否在跑、处理了多少事件。"""
    return {**_stats, "channel": CHANNEL, "trigger": TRIGGER,
            "merge_window_ms": int(MERGE_WINDOW * 1000),
            "thread_alive": bool(_thread and _thread.is_alive())}


def _drain(q: Queue) -> set[str]:
    out: set[str] = set()
    while True:
        try:
            out.add(q.get_nowait())
        except Empty:
            return out


def _process(ids: set[str]) -> None:
    """#14：投影只走 Outbox 一条路。NOTIFY 只用来唤醒 Dispatcher 立即 drain，
    不再在这里直接 resync_fact——否则同一事实被投影两次，两线程还会争 SYNC_LOCK。
    Outbox 事件由触发器同事务写入，drain 按聚合合并，延迟仍是亚秒级。"""
    from app.modules.ontology import outbox
    ok = fail = 0
    valid = [f for f in ids if f and _UUID_RE.match(f)]
    try:
        r = outbox.drain()
        ok, fail = int(r.get("processed") or 0), int(r.get("failed") or 0)
    except Exception as exc:                    # noqa: BLE001 Dispatcher 线程 1s 后兜底
        fail = len(valid)
        log.warning("outbox drain on notify failed: %s", exc)
    now = time.time()
    _stats.update({"batches": _stats["batches"] + 1,
                   "events": _stats["events"] + len(ids),
                   "processed": _stats["processed"] + ok,
                   "failed": _stats["failed"] + fail,
                   "last_batch": len(ids), "last_at": now})
    log.info("fact-changed batch: ids=%d ok=%d failed=%d", len(ids), ok, fail)


def _listen_loop() -> None:
    dsn = _dsn()
    while not _stop.is_set():
        conn = None
        try:
            # 每轮重连都复查一次触发器：startup 时数据库可能尚未就绪，
            # 或有人手工 DROP 过，这里能自愈，不会静默变成「假同步」。
            ensure_trigger()
            conn = psycopg.connect(dsn, autocommit=True)
            q: Queue = Queue()
            conn.add_notify_handler(lambda n: q.put(n.payload))
            conn.execute(f"LISTEN {CHANNEL}")
            _stats["connected"] = True
            _stats["listener_error"] = None
            log.info("LISTEN %s ready", CHANNEL)

            # 先 LISTEN 再补偿：两者之间没有丢事件的空窗。
            # 重连风暴保护：两次全量补偿至少间隔 CATCHUP_MIN_INTERVAL 秒（Outbox 账本本身不丢事件）。
            try:
                if time.time() - _stats.get("catchup_at", 0) < CATCHUP_MIN_INTERVAL:
                    raise _SkipCatchup()
                _stats["catchup_at"] = time.time()
                r = graph_sync.catchup_relations()
                _stats["catchup"] = r
                log.info("startup fact catchup: %s", r)
            except _SkipCatchup:
                pass
            except Exception as exc:            # noqa: BLE001 补偿失败不阻断监听
                log.warning("startup fact catchup failed: %s", exc)

            while not _stop.is_set():
                readable, _, _ = select.select([conn], [], [], IDLE_SELECT)
                if readable:
                    conn.execute("SELECT 1")     # 驱动 psycopg3 的 notify handler
                pending = _drain(q)
                if not pending:
                    continue
                deadline = time.monotonic() + MERGE_WINDOW
                while time.monotonic() < deadline:
                    readable, _, _ = select.select(
                        [conn], [], [], max(0.001, deadline - time.monotonic()))
                    if readable:
                        conn.execute("SELECT 1")
                    pending |= _drain(q)
                _process(pending)
        except Exception as exc:                # noqa: BLE001 断线/驱动异常都重连
            _stats["listener_error"] = f"{type(exc).__name__}: {exc}"
            log.warning("fact event listener disconnected (%s), retry in %.0fs",
                        exc, RETRY_WAIT)
        finally:
            _stats["connected"] = False
            if conn is not None:
                try:
                    conn.close()
                except Exception:               # noqa: BLE001
                    pass
        _stop.wait(RETRY_WAIT)


def start() -> None:
    """startup 调用：建触发器 → 起常驻监听线程（幂等，重复调用无副作用）。"""
    global _thread
    with _start_lock:
        if _thread and _thread.is_alive():
            return
        try:
            ensure_trigger()
        except Exception as exc:                # noqa: BLE001
            # 不阻断：监听线程每轮重连都会复查并重建，数据库晚就绪也能恢复
            _stats["listener_error"] = f"ensure_trigger: {exc}"
            log.warning("fact change trigger install deferred: %s", exc)
        _stop.clear()
        _thread = threading.Thread(target=_listen_loop, name="fact-event-sync",
                                   daemon=True)
        _thread.start()
        log.info("fact event sync started (trigger=%s)", TRIGGER)
