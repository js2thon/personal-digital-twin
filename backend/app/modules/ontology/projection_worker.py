"""SIL-EC-04 / EC-05：投影目标抽象（ProjectionWorker）与重建契约。

投影目标是**派生数据**，任何时刻都应能从「事实 + 实体」重建。本模块把
`resync_fact()` 收口到一个统一接口，好处是：

- 图与向量各有实现，可**单独**降级、单独重建、单独报告覆盖率；
- 事件分发（`outbox.dispatch`）不需要知道投影细节；
- 重建可以「先算目标态、再补缺删余」，不出现「清空后短暂无图」的空窗。

## 现状（2026-11-15）

| worker | 目标表 | 状态 |
|---|---|---|
| `graph` | `ontology.relations` | 可用，已上线 |
| `vector` | `knowledge.semantic_chunks.embedding_vec` | **不可用**：便携版 PG 无 `vector` 扩展，向量覆盖率 45/281（P0-1 阻塞） |

不可用的 worker **不参与分发**，也不把事件标成失败——否则整条账本会被一个
环境问题拖成死信。它的状态在 `registry()` 里显式暴露，符合 EC-04「不得返回
静默不完整结果」。
"""
from __future__ import annotations

import logging
import threading

from app.db import session as db
from app.modules.ontology import projection as proj
from app.modules.ontology import sync as graph_sync

log = logging.getLogger("twin.projection")

PROJECTION_ORIGIN = "fact_projection"
_REBUILD_LOCK = threading.Lock()
_state = {"rebuilding": None, "last_rebuild": None}


class ProjectionWorker:
    """投影目标的最小接口。实现方必须**幂等**：同一事件重复投递结果一致。"""

    name: str = ""
    targets: tuple[str, ...] = ()
    handles: tuple[str, ...] = ()
    available: bool = True
    unavailable_reason: str | None = None

    def handle(self, event: dict) -> dict:
        raise NotImplementedError

    def status(self) -> dict:
        return {"name": self.name, "targets": list(self.targets),
                "handles": list(self.handles), "available": self.available,
                "unavailable_reason": self.unavailable_reason}


class GraphProjectionWorker(ProjectionWorker):
    """事实 → `ontology.relations` 的确定性投影（已上线，0.13s 级）。"""

    name = "graph"
    targets = ("ontology.relations",)
    handles = ("fact.created", "fact.updated", "fact.withdrawn")

    def handle(self, event: dict) -> dict:
        if event["event_type"] not in self.handles:
            return {"applied": False, "reason": "not_handled"}
        r = graph_sync.resync_fact(event["aggregate_id"])
        return {"applied": True, "detail": r}

    def status(self) -> dict:
        base = super().status()
        row = db.fetch_one(
            "SELECT count(*) FILTER (WHERE status = 'active') AS active, "
            "       count(*) AS total FROM ontology.relations")
        base["coverage"] = {"active_edges": int((row or {}).get("active") or 0),
                            "total_edges": int((row or {}).get("total") or 0)}
        return base


class VectorProjectionWorker(ProjectionWorker):
    """事实 / 文档 → 向量索引。P0-1 未落地前显式降级，不静默返回部分结果。"""

    name = "vector"
    targets = ("knowledge.semantic_chunks",)
    handles = ("document.changed", "embedding.model_upgraded")

    def __init__(self) -> None:
        self._probe: dict | None = None

    def _detect(self) -> dict:
        """探测 pgvector 与向量覆盖率（60s 缓存，避免每次分发都查库）。"""
        import time

        if self._probe and time.time() - self._probe.get("at", 0) < 60:
            return self._probe
        try:
            ext = db.fetch_one(
                # #19：可安装 ≠ 已启用；必须查 pg_extension
                "SELECT count(*) AS n FROM pg_extension WHERE extname = 'vector'")
            chunks = db.fetch_one(
                "SELECT count(*) AS total, count(embedding_vec) AS embedded "
                "FROM knowledge.semantic_chunks")
            extension_available = int((ext or {}).get("n") or 0) > 0
            # The vector worker is intentionally not implemented yet. An
            # installed extension alone must not advertise a usable consumer.
            available = False
            reason = ("向量投影 Worker 尚未实现，当前不参与事件分发"
                      if extension_available else
                      "pgvector 扩展未启用（pg_extension 无 vector），"
                      "向量层为 TEXT 降级实现；对应 P0-1 未落地")
            probe = {"at": __import__("time").time(), "available": available,
                     "unavailable_reason": reason,
                     "coverage": {"chunks": int((chunks or {}).get("total") or 0),
                                  "embedded": int((chunks or {}).get("embedded") or 0)}}
        except Exception as exc:                # noqa: BLE001  探测失败按不可用处理
            probe = {"at": 0, "available": False,
                     "unavailable_reason": f"probe_failed: {type(exc).__name__}",
                     "coverage": {"chunks": 0, "embedded": 0}}
        self._probe = probe
        return probe

    @property
    def available(self) -> bool:                # type: ignore[override]
        return bool(self._detect()["available"])

    @property
    def unavailable_reason(self) -> str | None:  # type: ignore[override]
        return self._detect().get("unavailable_reason")

    def handle(self, event: dict) -> dict:
        if not self.available:
            return {"applied": False, "reason": "unavailable"}
        # 未实现不得报成功（否则事件被标 processed 后永久丢失）：抛错进入重试/死信，可 replay
        raise NotImplementedError("vector projection not implemented (P0-1)")

    def status(self) -> dict:
        probe = self._detect()
        return {"name": self.name, "targets": list(self.targets),
                "handles": list(self.handles), "available": probe["available"],
                "unavailable_reason": probe.get("unavailable_reason"),
                "coverage": probe.get("coverage")}


_WORKERS: list[ProjectionWorker] = [GraphProjectionWorker(), VectorProjectionWorker()]


def registry() -> list[dict]:
    out = []
    for w in _WORKERS:
        try:
            out.append(w.status())
        except Exception as exc:                # noqa: BLE001
            out.append({"name": w.name, "available": False,
                        "unavailable_reason": f"status_failed: {exc}"[:200]})
    return out


def dispatch(event: dict) -> dict:
    """把一条事件分发给所有可用 worker。全部跳过不算失败（记 skipped）。"""
    results: dict[str, dict] = {}
    errors: list[str] = []
    applied_any = False
    for w in _WORKERS:
        if event["event_type"] not in w.handles:
            continue
        if not w.available:
            results[w.name] = {"skipped": True, "reason": w.unavailable_reason}
            continue
        try:
            r = w.handle(event)
        except Exception as exc:                # noqa: BLE001
            results[w.name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            errors.append(f"{w.name}: {exc}")
            continue
        results[w.name] = r
        if r.get("applied"):
            applied_any = True
    if errors:
        return {"ok": False, "error": "; ".join(errors)[:1000], "workers": results}
    return {"ok": True, "applied": applied_any,
            "noop": not applied_any, "workers": results}


# ---------------------------------------------------------------- 对账
def reconcile(limit: int = 50) -> dict:
    """只读对账：账本积压 + 投影漂移 + 覆盖统计，给出 in_sync 判定。"""
    from app.modules.ontology import outbox

    box = outbox.stats()
    drift = graph_sync.orphans(limit=limit)
    drift_counts = drift["counts"]
    blocked = int(box["backlog"]) + int(box["dead"]) + sum(int(v) for v in drift_counts.values())
    return {
        "in_sync": blocked == 0,
        "outbox": {"total": box["total"], "backlog": box["backlog"],
                   "dead": box["dead"], "pending_lag_seconds": box["pending_lag_seconds"]},
        "drift": drift_counts,
        "drift_detail": drift if limit <= 50 else {},
        "workers": registry(),
        "rebuilding": _state["rebuilding"],
        "last_rebuild": _state["last_rebuild"],
        "verdict": ("一致" if blocked == 0 else
                    f"存在 {blocked} 项待收敛（积压/死信/漂移）"),
    }


# ---------------------------------------------------------------- 全量重建
def rebuild(worker: str = "graph", dry_run: bool = True) -> dict:
    """按 worker 全量重建派生数据。

    图的重建策略是**先算目标态、再补缺删余**，不清空重来：
    清空会让重建期间图查询返回空结果（EC-04 明确不允许静默不完整）。
    """
    if worker != "graph":
        status = next((w for w in registry() if w["name"] == worker), None)
        return {"worker": worker, "dry_run": dry_run, "supported": False,
                "reason": (status or {}).get("unavailable_reason")
                or "该投影目标尚未实现重建路径"}

    if not _REBUILD_LOCK.acquire(blocking=False):
        return {"worker": worker, "supported": True, "dry_run": dry_run,
                "status": "busy", "reason": "已有重建在进行"}
    try:
        _state["rebuilding"] = {"worker": worker, "started_at": _now()}
        with graph_sync.SYNC_LOCK:
            facts = db.fetch_all(
                "SELECT id::text AS id, claim_id, subject_id, predicate, object_value, "
                "       valid_from, status FROM knowledge.facts WHERE status = 'active'")
            entities = graph_sync._active_entities()
            desired = proj.project_facts(facts, entities)["edges"]
            # 比对口径：**所有** active 边都算「已有」。
            # 只比 fact_projection 会把「人工建边 + 已回写 backing fact」误判成缺失，
            # 于是插出一条重复边（relations 上没有唯一约束，插了就是真重复）。
            existing = db.fetch_all(
                "SELECT id::text AS id, source_entity_id::text AS s, relation_type AS rt, "
                "       target_entity_id::text AS t, COALESCE(properties->>'origin', '') AS origin "
                "FROM ontology.relations WHERE status = 'active'")
            # 但「多余」只在投影边里找：人工/历史边不归重建管（R3 不删非投影来源的边）。
            proj_edges = [r for r in existing if r["origin"] == PROJECTION_ORIGIN]
            want = {(str(e["source_entity_id"]), e["relation_type"],
                     str(e["target_entity_id"])) for e in desired}
            have = {(r["s"], r["rt"], r["t"]) for r in existing}
            to_add = [e for e in desired
                      if (str(e["source_entity_id"]), e["relation_type"],
                          str(e["target_entity_id"])) not in have]
            stale = [r["id"] for r in proj_edges if (r["s"], r["rt"], r["t"]) not in want]

            result = {"worker": worker, "dry_run": dry_run, "supported": True,
                      "facts": len(facts), "desired": len(desired),
                      "existing": len(existing), "existing_projected": len(proj_edges),
                      "to_add": len(to_add), "to_remove": len(stale)}
            if dry_run:
                return result

            added = 0
            for e in to_add:
                r = graph_sync._upsert_fact_edge_unlocked(str(e["fact_id"]))
                added += 1 if r.get("created") else 0
            removed = 0
            if stale:
                removed = graph_sync._soft_remove(
                    "id = ANY(CAST(:ids AS uuid[]))",
                    {"ids": "{" + ",".join(stale) + "}"}, "full_rebuild")
            result.update({"added": added, "removed": removed,
                           "after": graph_sync.status()})
            _state["last_rebuild"] = {**result, "finished_at": _now()}
            log.info("graph rebuild: facts=%d desired=%d added=%d removed=%d",
                     len(facts), len(desired), added, removed)
            return result
    finally:
        _state["rebuilding"] = None
        _REBUILD_LOCK.release()


def _now() -> float:
    import time

    return time.time()


def state() -> dict:
    return {"rebuilding": _state["rebuilding"], "last_rebuild": _state["last_rebuild"]}
