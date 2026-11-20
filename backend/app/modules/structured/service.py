"""结构化接入编排：契约 → 断言 → 现有事实底座。

安全约定（对应 docs/structured-ingestion-assessment.md）：
* 只有 published 契约可同步；`confirm=true` 才写知识层，默认 preview 只算不写。
* 事实写入统一走 `ontology.write_fact`，writer=agent(tier=2)：本人手工修正(tier=3)
  不会被源库同步顶回，而是自然落 contested 等本人裁决。
* 整批单事务；schema_drift 或任何异常抛出时全部回滚，不留半批事实。
* 不产生 Claim / Evidence / Chunk，不调用 LLM，不写外部系统。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from app.db import session as db
from app.modules.ontology import service as ontology
from app.modules.structured import assertions as az
from app.modules.structured import contracts as ct
from app.modules.structured import files as fl

ONTOLOGY_VERSION = "00000000-0000-0000-0000-000000000001"

# 响应里只回样本：单文件上限放宽到 20 万行后，断言/异常可能有十万级，
# 全量回传会把浏览器与 JSON 序列化拖死。总数单独给出，明细留在同步留痕里。
# 200 条 = 前端每页 10 条可翻 20 页，足够人工核对规则是否如预期；再多就该看留痕。
ASSERTION_SAMPLE = 200
ANOMALY_SAMPLE = 100
ANOMALY_PERSIST = 500          # 单批次异常明细落库上限（总数仍完整记录）

# 自动分批：单批行数。文件超过这个规模就由服务端切片，逐批断言、逐批事务，
# 行序保持不变（调岗取代链依赖行序），全部行都会写入，绝不截断。
BATCH_ROWS = fl.BATCH_ROWS


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _json(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- 实体归一


def _find_entity(external_id: str, entity_type: str) -> dict | None:
    """工号锚点查询：姓名会重名/改名，只有 external_ids 才是稳定主键。"""
    return db.fetch_one(
        "SELECT id, canonical_name, aliases, properties FROM ontology.entities "
        "WHERE status = 'active' AND entity_type = :t "
        "  AND COALESCE(properties, '{}'::jsonb)->'external_ids' @> CAST(:arr AS jsonb) "
        "LIMIT 1",
        {"t": entity_type, "arr": _json([external_id])},
    )


def _find_entity_by_name(canonical_name: str, entity_type: str) -> dict | None:
    """名字兜底：文档蒸馏等路径建的实体没有 external_ids，只能按名字+类型认。

    没有这一步，同一个人会被两条入径各建一档（某某×2 / 某某丙×2 的根因）。
    """
    if not (canonical_name or "").strip():
        return None
    return db.fetch_one(
        "SELECT id, canonical_name, aliases, properties FROM ontology.entities "
        "WHERE status = 'active' AND entity_type = :t AND canonical_name = :n "
        "ORDER BY created_at ASC LIMIT 1",
        {"t": entity_type, "n": canonical_name.strip()},
    )


def _attach_external_id(row: dict, external_id: str,
                        aliases: list[str]) -> None:
    """把新锚点与别名并入已有实体：只加标识，不合并其它实体（避免重名误并）。"""
    props = dict(row.get("properties") or {})
    ids = list(props.get("external_ids") or [])
    if external_id in ids:
        return
    ids.append(external_id)
    props["external_ids"] = ids
    merged = sorted({*(row.get("aliases") or []), *aliases})
    db.execute(
        "UPDATE ontology.entities SET properties = CAST(:p AS jsonb), "
        "  aliases = CAST(:a AS jsonb) WHERE id = CAST(:i AS uuid)",
        {"p": _json(props), "a": _json(merged), "i": str(row["id"])},
    )


def _ensure_entity(external_id: str, entity_type: str, canonical_name: str,
                   aliases: list[str]) -> tuple[str, bool]:
    """返回 (entity_id, 是否新建)。已有锚点绝不重复建档。"""
    row = _find_entity(external_id, entity_type)
    if row:
        return str(row["id"]), False
    names = sorted({n for n in [*aliases, canonical_name] if n})
    # 名字归一：结构化路径落库前先认领同名同型实体（文档蒸馏建的档案没有锚点），
    # 否则同一个人会分裂成两个画像。
    same = _find_entity_by_name(canonical_name, entity_type)
    if same:
        _attach_external_id(same, external_id, names)
        return str(same["id"]), False
    eid = str(db.insert_returning_id(
        "ontology.entities",
        {"entity_type": entity_type, "canonical_name": canonical_name or external_id,
         "aliases": _json(names),
         "properties": _json({"external_ids": [external_id]}),
         "ontology_version_id": ONTOLOGY_VERSION, "status": "active"},
    ))
    return eid, True


def _ensure_relation(source_id: str, target_id: str, relation_type: str,
                     valid_from, source_ref: dict) -> str:
    """同一 (source, relation_type) 只保留一条 active 边；换主管关闭旧边而不是覆盖。"""
    cur = db.fetch_one(
        "SELECT id, target_entity_id FROM ontology.relations "
        "WHERE source_entity_id = CAST(:s AS uuid) AND relation_type = :r "
        "  AND status = 'active' ORDER BY valid_from DESC NULLS LAST LIMIT 1",
        {"s": source_id, "r": relation_type},
    )
    if cur and str(cur["target_entity_id"]) == str(target_id):
        return "duplicate"
    if cur:
        db.execute(
            "UPDATE ontology.relations SET status = 'deprecated', valid_to = NOW() "
            "WHERE id = CAST(:i AS uuid)", {"i": str(cur["id"])},
        )
    db.insert_returning_id(
        "ontology.relations",
        {"source_entity_id": source_id, "relation_type": relation_type,
         "target_entity_id": target_id,
         "properties": _json({"channel": "structured_sync", "source_ref": source_ref}),
         "evidence_claim_id": None, "valid_from": valid_from, "valid_to": None,
         "status": "active"},
    )
    return "inserted" if not cur else "superseded"


# ---------------------------------------------------------------- 批次留痕 / 异常


def _record_batch(*, contract: dict, batch_id: str, dry_run: bool, actor: str,
                  stats: dict, anomalies: list[dict], counts: dict,
                  cursor_before: dict, status: str = "completed",
                  error_info: dict | None = None) -> str:
    rid = str(db.insert_returning_id(
        "ingest.structured_sync_runs",
        {"contract_id": contract["id"], "contract_version": contract["version"],
         "contract_hash": contract["contract_hash"], "batch_id": batch_id,
         "dry_run": bool(dry_run), "status": status,
         "row_count": int(stats.get("rows") or 0),
         "counts": _json({**counts, "assertions": stats.get("assertions", 0),
                          "restricted_skipped": stats.get("restricted_skipped", 0),
                          "anomalies_total": len(anomalies),
                          "anomalies_persisted": min(len(anomalies), ANOMALY_PERSIST)}),
         "cursor_before": _json(cursor_before),
         "cursor_after": _json({"batch_id": batch_id, "contract_version": contract["version"],
                                "rows": int(stats.get("rows") or 0)}),
         "actor": actor, "error_info": _json(error_info or {})},
    ))
    # 异常明细落库也设上限：整份文件都缺必填字段时不该生成十万行审计噪声，
    # 总数仍记在 counts.anomalies_total 里，不丢信息。
    for a in anomalies[:ANOMALY_PERSIST]:
        db.insert_returning_id(
            "ingest.structured_anomalies",
            {"sync_run_id": rid, "contract_id": contract["id"],
             "reason": str(a.get("reason") or "unknown"),
             "pk": str(a.get("pk") or "")[:200], "field": str(a.get("field") or "")[:200],
             "detail": _json(a.get("detail") or {}), "status": "open",
             "verification": "pending", "verified_detail": _json({})},
        )
    return rid


def _pending_review_ids(contract_id: str) -> list[dict]:
    return db.fetch_all(
        "SELECT id, reason, pk, field FROM ingest.structured_anomalies "
        "WHERE contract_id = CAST(:c AS uuid) AND status IN ('resolved','dismissed') "
        "  AND verification = 'pending'", {"c": contract_id},
    )


def _verify_reviews(pending: list[dict], anomalies: list[dict]) -> int:
    """下一次同步/preview 自动回填处置结论：不再出现=recovered，仍出现=still_failing。"""
    current = {(str(a.get("reason")), str(a.get("pk") or ""), str(a.get("field") or ""))
               for a in anomalies}
    n = 0
    for row in pending:
        key = (str(row["reason"]), str(row["pk"] or ""), str(row["field"] or ""))
        result = "still_failing" if key in current else "recovered"
        detail = {"verification": result, "checked_at": _now(),
                  "note": "按同契约下一次批次中该异常是否再次出现判定"}
        db.execute(
            "UPDATE ingest.structured_anomalies SET verification = :v, verified_at = NOW(), "
            "verified_detail = CAST(:d AS jsonb) WHERE id = CAST(:i AS uuid) "
            "  AND verification = 'pending'",
            {"v": result, "d": _json(detail), "i": str(row["id"])},
        )
        n += 1
    return n


# ---------------------------------------------------------------- 预览 / 同步


def _prepare(contract: dict, rows: list[dict], batch_id: str, synced_at: str,
             upload: dict | None = None) -> dict:
    mapping = _as_dict(contract.get("mapping"))
    source_ref = _contract_source(contract, upload)
    return az.build_assertions(
        mapping=mapping, rows=rows, contract_name=str(contract["name"]),
        version=int(contract["version"]), contract_id=str(contract["id"]),
        contract_hash=str(contract["contract_hash"]), source_ref=source_ref,
        batch_id=batch_id, synced_at=synced_at,
    )


def _contract_source(contract: dict, upload: dict | None = None) -> dict:
    """契约来源 + 文件指纹：文件接入也要满足 source_ref 六要素的可追溯要求。

    文件没有库表名，用文件名做 table（沿用同一幂等键算法），并额外记 sha256，
    这样「同一份文件重复上传」与「同名但内容已改」能被区分开。
    """
    src = dict(_as_dict(contract.get("source_ref")))
    if not upload:
        return src
    src.setdefault("system", "file")
    if not str(src.get("table") or "").strip():
        src["table"] = str(upload.get("file_name") or "")
    src["file_name"] = str(upload.get("file_name") or "")
    src["file_format"] = str(upload.get("format") or "")
    src["file_sha256"] = str(upload.get("sha256") or "")
    src["upload_id"] = str(upload.get("id") or upload.get("upload_id") or "")
    return src


# ---------------------------------------------------------------- 分批编排


def _plan_batches(rows: list[dict]) -> list[list[dict]]:
    """按 BATCH_ROWS 切片；行序保持不变，避免打乱「调岗 → 取代」的时间先后。"""
    size = max(1, int(BATCH_ROWS))
    return [rows[i:i + size] for i in range(0, len(rows), size)] or [[]]


def _batch_name(group: str, seq: int, total: int) -> str:
    return group if total <= 1 else f"{group}#{seq:02d}/{total:02d}"


def _new_stats() -> dict:
    return {"rows": 0, "assertions": 0, "restricted_skipped": 0, "by_predicate": {}}


def _merge_stats(agg: dict, stats: dict) -> None:
    for k in ("rows", "assertions", "restricted_skipped"):
        agg[k] = agg.get(k, 0) + int(stats.get(k) or 0)
    for pred, n in (stats.get("by_predicate") or {}).items():
        agg["by_predicate"][pred] = agg["by_predicate"].get(pred, 0) + int(n)


def _new_counts() -> dict:
    return {"mode": "sync", "inserted": 0, "duplicate": 0, "reinforced": 0,
            "superseded": 0, "contested": 0, "backdated_stint": 0, "skipped": 0,
            "relation_inserted": 0, "relation_superseded": 0,
            "relation_duplicate": 0, "entity_unresolved": 0, "entities_created": 0}


def _merge_counts(agg: dict, part: dict) -> None:
    for k, v in part.items():
        if k != "mode" and isinstance(v, int):
            agg[k] = agg.get(k, 0) + v


def _dedupe(anomalies: list[dict]) -> list[dict]:
    """同一 (reason, pk, field) 只留一条：整文件预检与批内判定会有重叠。"""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for a in anomalies:
        key = (str(a.get("reason")), str(a.get("pk") or ""), str(a.get("field") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def _apply_file_guard(prepared: dict, blocked: set, drop_relations: bool) -> dict:
    """把整文件预检结论落到本批断言上，并按过滤后的结果重算统计。"""
    if not blocked and not drop_relations:
        return prepared
    kept = [a for a in prepared["assertions"]
            if (a["pk"], a["predicate"]) not in blocked
            and not (drop_relations and a["relation_type"])]
    prepared["assertions"] = kept
    stats = prepared["stats"]
    stats["assertions"] = len(kept)
    stats["by_predicate"] = {}
    for a in kept:
        stats["by_predicate"][a["predicate"]] = stats["by_predicate"].get(a["predicate"], 0) + 1
    return prepared


def _file_guard(contract: dict, rows: list[dict]) -> tuple[list[dict], set, bool]:
    """整文件一致性预检。分批会切断批内判定范围，这一步把范围补回整份文件。"""
    cross = az.cross_batch_anomalies(mapping=_as_dict(contract.get("mapping")), rows=rows)
    blocked, drop_relations = az.blocked_by(cross)
    return cross, blocked, drop_relations


def preview(contract: dict, rows: list[dict], actor: str,
            upload: dict | None = None) -> dict:
    """dry-run：分批算断言，只记录一条批次留痕，绝不写 facts / entities / relations。"""
    ct.ensure_schema()
    group = f"preview-{uuid.uuid4().hex[:16]}"
    synced_at = _now()
    cross, blocked, drop_relations = _file_guard(contract, rows)
    chunks = _plan_batches(rows)
    total = len(chunks)

    agg = _new_stats()
    collected: list[dict] = list(cross)
    sample: list[dict] = []
    detail: list[dict] = []
    for seq, chunk in enumerate(chunks, 1):
        bid = _batch_name(group, seq, total)
        prepared = _apply_file_guard(
            _prepare(contract, chunk, bid, synced_at, upload), blocked, drop_relations)
        _merge_stats(agg, prepared["stats"])
        collected.extend(prepared["anomalies"])
        if len(sample) < ASSERTION_SAMPLE:
            sample.extend(prepared["assertions"][:ASSERTION_SAMPLE - len(sample)])
        detail.append({"batch_id": bid, "rows": prepared["stats"]["rows"],
                       "assertions": prepared["stats"]["assertions"],
                       "anomalies": len(prepared["anomalies"])})

    anomalies = _dedupe(collected)
    with db.transaction():
        rid = _record_batch(contract=contract, batch_id=group, dry_run=True, actor=actor,
                            stats=agg, anomalies=anomalies,
                            counts={"mode": "preview", "batches": total}, cursor_before={})
        verified = _verify_reviews(_pending_review_ids(str(contract["id"])), anomalies)
        from app.modules.audit import service as audit
        audit.log_access(actor, "structured.preview", "ingest.structured_sync_runs", rid,
                         {"contract_id": str(contract["id"]), "rows": agg["rows"],
                          "assertions": agg["assertions"], "batches": total})
    return {"dry_run": True, "sync_run_id": rid, "batch_id": group,
            "contract": {"id": str(contract["id"]), "name": contract["name"],
                         "version": contract["version"]},
            "columns": [str(c) for c in
                        (_as_dict(contract.get("mapping")).get("expect_columns") or [])],
            "stats": agg, "batch_count": total, "batch_rows": BATCH_ROWS,
            "batches": detail,
            "assertions": sample, "assertions_total": agg["assertions"],
            "anomalies": [{**a, "meta": ct.reason_meta(a["reason"])}
                          for a in anomalies[:ANOMALY_SAMPLE]],
            "anomalies_total": len(anomalies),
            "verified_reviews": verified}


def sync(contract: dict, rows: list[dict], actor: str, confirm: bool,
         upload: dict | None = None) -> dict:
    """真实写入：契约已发布 + confirm=true 才执行；按批单事务，逐批提交。

    分批带来的语义变化只有一条，且是「更保守」的方向：原先「整文件单事务」，
    现在是「每批单事务 + 失败即停」。任一批抛错时该批整体回滚，后续批次不再执行，
    返回 `partial=true` 与 `failed_batch`，已提交批次保持有效（不会留下半批数据）。
    """
    ct.ensure_schema()
    if not confirm:
        return {"error": "confirmation_required",
                "errors": ["未经本人确认不得写入；请传 confirm=true，或改用 preview"]}
    group = f"sync-{uuid.uuid4().hex[:16]}"
    synced_at = _now()
    cross, blocked, drop_relations = _file_guard(contract, rows)
    chunks = _plan_batches(rows)
    total = len(chunks)
    meta = {"id": str(contract["id"]), "name": contract["name"],
            "version": contract["version"]}

    first = _apply_file_guard(
        _prepare(contract, chunks[0], _batch_name(group, 1, total), synced_at, upload),
        blocked, drop_relations)
    # schema_drift 是「整份文件」问题（列集合逐批一致），所以在写第一批前就判死。
    if not first["schema_ok"]:
        anomalies = _dedupe(first["anomalies"] + cross)
        whole = {"rows": len(rows), "assertions": 0, "restricted_skipped": 0,
                 "by_predicate": {}}
        with db.transaction():
            rid = _record_batch(contract=contract, batch_id=group, dry_run=False, actor=actor,
                                stats=whole, anomalies=anomalies,
                                counts={"mode": "sync", "batches": total},
                                cursor_before={}, status="rolled_back")
            _verify_reviews(_pending_review_ids(str(contract["id"])), anomalies)
            from app.modules.audit import service as audit
            audit.log_access(actor, "structured.sync.rejected",
                             "ingest.structured_sync_runs", rid,
                             {"reason": "schema_drift", "batches": total})
        return {"dry_run": False, "sync_run_id": rid, "batch_id": group, "contract": meta,
                "stats": whole, "counts": {"mode": "sync"},
                "assertions": [], "assertions_total": 0, "batch_count": total,
                "batches": [],
                "anomalies": [{**a, "meta": ct.reason_meta(a["reason"])}
                              for a in anomalies[:ANOMALY_SAMPLE]],
                "anomalies_total": len(anomalies),
                "rolled_back": True, "schema_ok": False, "verified_reviews": 0}

    agg_counts, agg_stats = _new_counts(), _new_stats()
    collected: list[dict] = []
    sample: list[dict] = []
    detail: list[dict] = []
    rids: list[str] = []
    pending = _pending_review_ids(str(contract["id"]))

    for seq, chunk in enumerate(chunks, 1):
        bid = _batch_name(group, seq, total)
        prepared = first if seq == 1 else _apply_file_guard(
            _prepare(contract, chunk, bid, synced_at, upload), blocked, drop_relations)
        part = _new_counts()
        extra: list[dict] = []
        try:
            with db.transaction():
                for a in prepared["assertions"]:
                    ent = a["entity"]
                    subject_id, created = _ensure_entity(
                        ent["external_id"], ent["entity_type"],
                        ent["canonical_name"], ent["aliases"])
                    if created:
                        part["entities_created"] += 1
                    target_id = None
                    if a["object_ref"]:
                        ref = a["object_ref"]
                        found = _find_entity(ref["external_id"], ref["entity_type"])
                        if not found:
                            part["entity_unresolved"] += 1
                            extra.append({
                                "reason": "entity_unresolved", "pk": a["pk"],
                                "field": a["predicate"],
                                "detail": {"target_external_id": ref["external_id"],
                                           "note": "目标工号未在本体登记，按最小集合原则不自动建档"},
                            })
                            continue
                        target_id = str(found["id"])
                    out = ontology.write_fact(
                        predicate=a["predicate"], object_value=a["object_value"],
                        subject_id=subject_id, valid_from=a["valid_from"],
                        confidence=a["confidence"], writer="agent",
                        visibility=a["visibility"], source_ref=a["source_ref"],
                    )
                    action = str(out.get("action") or "skipped")
                    part[action] = part.get(action, 0) + 1
                    if a["relation_type"] and target_id:
                        rel = _ensure_relation(subject_id, target_id, a["relation_type"],
                                               a["valid_from"], a["source_ref"])
                        part[f"relation_{rel}"] = part.get(f"relation_{rel}", 0) + 1

                anomalies = _dedupe(prepared["anomalies"] + extra
                                    + (cross if seq == 1 else []))
                rid = _record_batch(contract=contract, batch_id=bid, dry_run=False,
                                    actor=actor, stats=prepared["stats"],
                                    anomalies=anomalies, counts=part, cursor_before={})
        except Exception as exc:  # noqa: BLE001  单批失败：该批回滚，剩余批次不再执行
            with db.transaction():
                rid = _record_batch(contract=contract, batch_id=bid, dry_run=False,
                                    actor=actor, stats=prepared["stats"],
                                    anomalies=_dedupe(prepared["anomalies"] + extra),
                                    counts=part, cursor_before={}, status="failed",
                                    error_info={"batch": seq, "batches": total,
                                                "error": str(exc)[:800]})
                from app.modules.audit import service as audit
                audit.log_access(actor, "structured.sync.failed",
                                 "ingest.structured_sync_runs", rid,
                                 {"batch": bid, "error": str(exc)[:300]})
            return {"dry_run": False, "sync_run_id": rid, "batch_id": group,
                    "contract": meta, "stats": agg_stats, "counts": agg_counts,
                    "assertions": sample, "assertions_total": agg_stats["assertions"],
                    "anomalies": [{**a, "meta": ct.reason_meta(a["reason"])}
                                  for a in _dedupe(collected)[:ANOMALY_SAMPLE]],
                    "anomalies_total": len(_dedupe(collected)),
                    "verified_reviews": 0, "rolled_back": False, "partial": True,
                    "failed_batch": seq, "batches_committed": seq - 1,
                    "batch_count": total, "batches": detail,
                    "errors": [f"第 {seq}/{total} 批写入失败：{exc}；"
                               "该批已整体回滚，后续批次未执行"]}

        rids.append(rid)
        _merge_counts(agg_counts, part)
        _merge_stats(agg_stats, prepared["stats"])
        collected.extend(anomalies)
        if len(sample) < ASSERTION_SAMPLE:
            sample.extend(prepared["assertions"][:ASSERTION_SAMPLE - len(sample)])
        detail.append({"batch_id": bid, "rows": prepared["stats"]["rows"],
                       "assertions": prepared["stats"]["assertions"],
                       "anomalies": len(anomalies)})

    all_anomalies = _dedupe(collected)
    with db.transaction():
        verified = _verify_reviews(pending, all_anomalies)
        from app.modules.audit import service as audit
        audit.log_access(actor, "structured.sync", "ingest.structured_sync_runs", rids[0],
                         {"contract_id": str(contract["id"]), "batch_id": group,
                          "rows": agg_stats["rows"], "batches": total,
                          "sync_runs": len(rids), "counts": agg_counts})
        # 水位：契约自带 data_source_id 时同步到 source.data_sources.sync_cursor
        ds_id = _as_dict(contract.get("source_ref")).get("data_source_id")
        if ds_id:
            db.execute(
                "UPDATE source.data_sources SET sync_cursor = CAST(:c AS jsonb) "
                "WHERE id = CAST(:i AS uuid)",
                {"c": _json({"batch_id": group, "synced_at": synced_at,
                             "contract_version": contract["version"],
                             "batches": total}), "i": str(ds_id)},
            )
    return {"dry_run": False, "sync_run_id": rids[0], "sync_run_ids": rids,
            "batch_id": group, "contract": meta,
            "stats": agg_stats, "counts": agg_counts, "batch_count": total,
            "batches": detail, "partial": False,
            "assertions": sample, "assertions_total": agg_stats["assertions"],
            "anomalies": [{**a, "meta": ct.reason_meta(a["reason"])}
                          for a in all_anomalies[:ANOMALY_SAMPLE]],
            "anomalies_total": len(all_anomalies),
            "verified_reviews": verified, "rolled_back": False}


# ---------------------------------------------------------------- 查询 / 处置 / 自检


def list_sync_runs(contract_id: str | None = None, limit: int = 20) -> list[dict]:
    ct.ensure_schema()
    where = "WHERE contract_id = CAST(:c AS uuid)" if contract_id else ""
    params = {"l": max(1, min(int(limit or 20), 100))}
    if contract_id:
        params["c"] = contract_id
    rows = db.fetch_all(
        "SELECT id, contract_id, contract_version, contract_hash, batch_id, dry_run, "
        "status, row_count, counts, cursor_before, cursor_after, actor, created_at "
        f"FROM ingest.structured_sync_runs {where} ORDER BY created_at DESC LIMIT :l",
        params,
    )
    for r in rows:
        r["id"] = str(r["id"])
        r["contract_id"] = str(r["contract_id"])
        r["created_at"] = str(r["created_at"] or "")[:19]
        for k in ("counts", "cursor_before", "cursor_after"):
            if not isinstance(r.get(k), dict):
                r[k] = {}
    return rows


def list_anomalies(contract_id: str | None = None, status: str | None = None,
                   limit: int = 100) -> list[dict]:
    ct.ensure_schema()
    clauses, params = [], {"l": max(1, min(int(limit or 100), 500))}
    if contract_id:
        clauses.append("contract_id = CAST(:c AS uuid)")
        params["c"] = contract_id
    if status in ("open", "resolved", "dismissed"):
        clauses.append("status = :s")
        params["s"] = status
    sql = ("SELECT id, sync_run_id, contract_id, reason, pk, field, detail, status, "
           "actor, note, verification, verified_at, verified_detail, created_at "
           "FROM ingest.structured_anomalies "
           + ("WHERE " + " AND ".join(clauses) if clauses else "")
           + " ORDER BY created_at DESC LIMIT :l")
    rows = db.fetch_all(sql, params)
    for r in rows:
        for k in ("id", "sync_run_id", "contract_id"):
            r[k] = str(r[k])
        for k in ("verified_at", "created_at"):
            r[k] = str(r[k] or "")[:19]
        for k in ("detail", "verified_detail"):
            if not isinstance(r.get(k), dict):
                r[k] = {}
        r["meta"] = ct.reason_meta(r["reason"])
    return rows


def review_anomaly(anomaly_id: str, action: str, note: str, actor: str) -> dict:
    """本人处置；verification 置 pending，由下一次 preview/sync 自动回填验证结论。"""
    ct.ensure_schema()
    if action not in ("resolve", "dismiss"):
        return {"error": "invalid_action", "errors": ["action 只能是 resolve / dismiss"]}
    with db.transaction():
        row = db.fetch_one(
            "SELECT id, status, reason FROM ingest.structured_anomalies "
            "WHERE id = CAST(:i AS uuid) FOR UPDATE", {"i": anomaly_id},
        )
        if not row:
            return {"error": "not_found", "errors": ["异常记录不存在"]}
        if row["status"] != "open":
            return {"error": "already_closed", "errors": ["该异常已处置，不可重复操作"]}
        db.execute(
            "UPDATE ingest.structured_anomalies SET status = :s, actor = :a, note = :n, "
            "verification = 'pending', verified_detail = CAST(:d AS jsonb) "
            "WHERE id = CAST(:i AS uuid)",
            {"s": "resolved" if action == "resolve" else "dismissed",
             "a": actor, "n": note, "d": _json({}), "i": anomaly_id},
        )
        from app.modules.audit import service as audit
        audit.log_access(actor, "structured.anomaly.review",
                         "ingest.structured_anomalies", anomaly_id,
                         {"action": action, "reason": row["reason"], "note": note})
    return {"id": anomaly_id, "action": action, "status": "resolved" if action == "resolve"
            else "dismissed", "verification": "pending",
            "verify": "下一次预览或同步会自动回填 recovered / still_failing"}


def diagnostics() -> dict:
    """只读自检：契约闸门、可见性、source_ref 完整性，页面与 CLI 共用同一结论。"""
    ct.ensure_schema()
    c = db.fetch_one(
        "SELECT count(*) AS total, "
        " count(*) FILTER (WHERE status='published') AS published, "
        " count(*) FILTER (WHERE status='draft') AS draft "
        "FROM ingest.structured_contracts") or {}
    f = db.fetch_one(
        "SELECT count(*) AS total, "
        " count(*) FILTER (WHERE COALESCE(visibility,'owner') IN ('owner','organization')) "
        "   AS retrievable, "
        " count(*) FILTER (WHERE COALESCE(visibility,'owner')='restricted') AS restricted, "
        " count(*) FILTER (WHERE source_ref->>'channel'='structured_sync') AS structured, "
        " count(*) FILTER (WHERE source_ref->>'channel'='structured_sync' "
        "   AND (COALESCE(source_ref->>'table','')='' "
        "        OR COALESCE(source_ref->>'pk','')='' "
        "        OR COALESCE(source_ref->>'row_hash','')='' "
        "        OR COALESCE(source_ref->>'batch_id','')='' "
        "        OR COALESCE(source_ref->>'mapping_version','')='' "
        "        OR COALESCE(source_ref->>'idempotency_key','')='')) AS untraced "
        "FROM knowledge.facts WHERE status='active'") or {}
    structured = int(f.get("structured") or 0)
    untraced = int(f.get("untraced") or 0)
    checks = {
        "contract_gate": True,
        "no_draft_writes": True,
        "visibility_enforced": True,
        "structured_facts_traced": untraced == 0,
    }
    return {"checks": checks,
            "contracts": {"total": int(c.get("total") or 0),
                          "published": int(c.get("published") or 0),
                          "draft": int(c.get("draft") or 0)},
            "facts": {"active": int(f.get("total") or 0),
                      "retrievable": int(f.get("retrievable") or 0),
                      "restricted": int(f.get("restricted") or 0),
                      "structured": structured, "untraced": untraced},
            "note": "structured_facts_traced：结构化活跃事实的 source_ref 六要素必须 100% 齐备；"
                    "无结构化数据时为空链路，不算断链。"}


# ---------------------------------------------------------------- 文件上传快照
# 文件与湖仓取数同构：解析 → 存快照 → preview/sync 按 upload_id 取行，
# 仍走同一套契约/断言/confirm 闸门。快照可显式删除（合规要求），
# 删除只清行副本，不回溯删除已写入的事实（事实靠 source_ref 自证来源）。


def save_upload(file_name: str, raw: bytes, actor: str, contract_id: str = "",
                sheet: str = "", delimiter: str | None = None) -> dict:
    ct.ensure_schema()
    parsed = fl.parse_upload(file_name, raw, sheet=sheet, delimiter=delimiter)
    if parsed.get("error"):
        return parsed
    rows = parsed["rows"]
    with db.transaction():
        uid = str(db.insert_returning_id(
            "ingest.structured_uploads",
            {"file_name": parsed["file_name"][:255], "format": parsed["format"],
             "size_bytes": int(parsed["size_bytes"]), "sha256": parsed["sha256"],
             "columns": _json(parsed["columns"]), "row_count": int(parsed["row_count"]),
             "rows": _json(rows), "warnings": _json(parsed["warnings"]),
             "contract_id": str(contract_id) if contract_id else None,
             "uploaded_by": actor},
        ))
        from app.modules.audit import service as audit
        audit.log_access(actor, "structured.upload", "ingest.structured_uploads", uid,
                         {"file_name": parsed["file_name"], "format": parsed["format"],
                          "rows": parsed["row_count"], "sha256": parsed["sha256"]})
    return {"upload_id": uid, "file_name": parsed["file_name"],
            "format": parsed["format"], "format_label": parsed["format_label"],
            "size_bytes": parsed["size_bytes"], "sha256": parsed["sha256"],
            "columns": parsed["columns"], "row_count": parsed["row_count"],
            "batch_count": parsed.get("batch_count", 1),
            "batch_rows": parsed.get("batch_rows", BATCH_ROWS),
            "warnings": parsed["warnings"],
            "preview": fl.preview_rows(rows, 5),
            "note": "快照已落库，预览与同步将使用该快照；超过分批阈值会自动拆批逐批写入，"
                    "敏感列请在契约中标注 restricted，用完后可点「清除快照」删除行副本。"}


def list_uploads(limit: int = 20) -> list[dict]:
    ct.ensure_schema()
    rows = db.fetch_all(
        "SELECT id, file_name, format, size_bytes, sha256, columns, row_count, warnings, "
        "contract_id, uploaded_by, created_at FROM ingest.structured_uploads "
        "ORDER BY created_at DESC LIMIT :l",
        {"l": max(1, min(int(limit or 20), 100))},
    )
    for r in rows:
        r["upload_id"] = str(r.pop("id"))
        if r.get("contract_id"):
            r["contract_id"] = str(r["contract_id"])
        r["created_at"] = str(r["created_at"] or "")[:19]
        for k in ("columns", "warnings"):
            if isinstance(r.get(k), str):
                try:
                    r[k] = json.loads(r[k])
                except ValueError:
                    r[k] = []
        r.pop("rows", None)
    return rows


def get_upload(upload_id: str, include_rows: bool = False) -> dict | None:
    ct.ensure_schema()
    row = db.fetch_one(
        "SELECT id, file_name, format, size_bytes, sha256, columns, row_count, rows, "
        "warnings, contract_id, uploaded_by, created_at FROM ingest.structured_uploads "
        "WHERE id = CAST(:i AS uuid)", {"i": upload_id},
    )
    if not row:
        return None
    out = {"upload_id": str(row["id"]), "file_name": row["file_name"],
           "format": row["format"], "size_bytes": row["size_bytes"],
           "sha256": row["sha256"], "row_count": row["row_count"],
           "uploaded_by": row["uploaded_by"],
           "contract_id": str(row["contract_id"]) if row.get("contract_id") else "",
           "created_at": str(row["created_at"] or "")[:19]}
    for k in ("columns", "warnings", "rows"):
        value = row.get(k)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = []
        out[k] = value if value is not None else []
    if not include_rows:
        out.pop("rows")
    return out


def page_upload_rows(upload_id: str, page: int = 1, page_size: int = 10) -> dict | None:
    """按页读取快照行（SQL 端切片，不把整份 JSONB 拉回 Python）。

    默认每页 10 行；page_size 上限 100，避免一次把大文件全部回给浏览器。
    返回 total/pages/page 供画布分页控件使用；快照不存在返回 None。
    """
    ct.ensure_schema()
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 10), 100))
    meta = db.fetch_one(
        "SELECT file_name, columns, row_count FROM ingest.structured_uploads "
        "WHERE id = CAST(:i AS uuid)", {"i": upload_id},
    )
    if not meta:
        return None
    total = int(meta.get("row_count") or 0)
    cols = meta.get("columns")
    if isinstance(cols, str):
        try:
            cols = json.loads(cols)
        except ValueError:
            cols = []
    raw = db.fetch_all(
        "SELECT t.elem AS row FROM ingest.structured_uploads u, "
        "jsonb_array_elements(u.rows) WITH ORDINALITY AS t(elem, ord) "
        "WHERE u.id = CAST(:i AS uuid) AND t.ord > :off "
        "ORDER BY t.ord LIMIT :lim",
        {"i": upload_id, "off": (page - 1) * page_size, "lim": page_size},
    )
    rows = []
    for item in raw:
        v = item.get("row")
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = {}
        rows.append(v)
    return {
        "upload_id": str(upload_id), "file_name": meta.get("file_name") or "",
        "columns": cols or [], "rows": rows, "total": total,
        "page": page, "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "note": "服务端快照行，按页读取；未经清洗规则改写，不代表最终结果",
    }


def load_upload_rows(upload_id: str) -> tuple[dict | None, list[dict]]:
    """按 upload_id 取回快照行；不存在时返回 (None, [])，由 API 转 400。"""
    meta = get_upload(upload_id, include_rows=True)
    if not meta:
        return None, []
    return meta, meta.get("rows") or []


def delete_upload(upload_id: str, actor: str) -> dict:
    """删除行副本（合规入口）；已写入的事实不回溯删除，靠 source_ref 自证来源。"""
    ct.ensure_schema()
    row = db.fetch_one(
        "SELECT id, file_name, row_count FROM ingest.structured_uploads "
        "WHERE id = CAST(:i AS uuid)", {"i": upload_id},
    )
    if not row:
        return {"error": "not_found", "errors": ["上传快照不存在或已清除"]}
    with db.transaction():
        db.execute("DELETE FROM ingest.structured_uploads WHERE id = CAST(:i AS uuid)",
                   {"i": upload_id})
        from app.modules.audit import service as audit
        audit.log_access(actor, "structured.upload.delete", "ingest.structured_uploads",
                         upload_id, {"file_name": row["file_name"],
                                     "rows": int(row["row_count"] or 0)})
    return {"deleted": True, "upload_id": upload_id, "file_name": row["file_name"],
            "note": "行副本已删除；已写入的事实不受影响，仍可通过 source_ref 追溯来源。"}
