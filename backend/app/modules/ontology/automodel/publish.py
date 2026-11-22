"""发布：本体草案 → cq_v2 Schema Registry；自动指标 → ontology.metric_definitions。

- Registry：合并到当前版本（新增类/关系/属性），按 registry.publish 生成 diff / 兼容性 / 迁移计划，
  并发 `ontology.published` Outbox 事件（供重投影、质量规则、CQ 回归消费）。
- 指标：转换成已有 metric_engine 的定义格式（native/derived/composite + formula），
  先跑 metric_engine.validate，再按 (tenant, metric_id, version) 入库，状态 draft；
  审批 / 发布走已有 /ontology/metrics/{id}/approve|publish 端点，不绕过审核。
"""
from __future__ import annotations

import copy
import json

AGG_MAP = {"count": "count", "sum": "sum", "avg": "avg", "max": "max",
           "count_distinct": "distinct_count", "count_nonnull": "count", "count_links": "count"}


def merge_schema(current: dict, draft: dict, version: str) -> dict:
    """字段级合并本体版本；业务对象身份/接口/多源绑定不再被旧 Registry 整体覆盖。"""
    out = copy.deepcopy(current or {})
    conflicts = []

    def unique(items):
        return list({json.dumps(x, sort_keys=True, ensure_ascii=False): x for x in items}.values())

    for section in ("entity_types", "relation_types", "attributes", "interfaces"):
        out.setdefault(section, {})
        for name, proposed in (draft.get(section) or {}).items():
            candidate = copy.deepcopy(proposed)
            if name not in out[section]:
                out[section][name] = candidate
                continue
            existing = out[section][name]
            if section == "entity_types":
                # 核心身份/对象类型冲突必须人工裁决；非冲突元数据按集合并集补齐。
                for field in ("object_kind", "parent", "title_property"):
                    old, new = existing.get(field), candidate.get(field)
                    if old and new and old != new:
                        conflicts.append({"section": section, "ref": name, "field": field,
                                          "current": old, "draft": new})
                merged = {**candidate, **existing}
                for field in ("aliases", "interfaces"):
                    merged[field] = unique((existing.get(field) or []) + (candidate.get(field) or []))
                merged["identity_rules"] = unique((existing.get("identity_rules") or []) +
                                                  (candidate.get("identity_rules") or []))
                merged["backing_sources"] = unique((existing.get("backing_sources") or []) +
                                                    (candidate.get("backing_sources") or []))
                old_attrs = {x.get("name"): x for x in existing.get("attributes") or []}
                for item in candidate.get("attributes") or []:
                    key = item.get("name")
                    if key not in old_attrs:
                        old_attrs[key] = item
                    elif old_attrs[key].get("range") != item.get("range"):
                        conflicts.append({"section": section, "ref": name,
                                          "field": f"attributes.{key}.range",
                                          "current": old_attrs[key].get("range"),
                                          "draft": item.get("range")})
                merged["attributes"] = list(old_attrs.values())
                merged["discovery_evidence"] = candidate.get("discovery_evidence") or existing.get("discovery_evidence")
                out[section][name] = merged
            elif section == "relation_types":
                if (existing.get("source"), existing.get("target")) != (
                        candidate.get("source"), candidate.get("target")):
                    conflicts.append({"section": section, "ref": name,
                                      "field": "endpoints", "current": existing, "draft": candidate})
                    continue
                merged = {**candidate, **existing}
                merged["backing_sources"] = unique((existing.get("backing_sources") or []) +
                                                    (candidate.get("backing_sources") or []))
                merged["properties"] = unique((existing.get("properties") or []) +
                                               (candidate.get("properties") or []))
                out[section][name] = merged
            elif section == "attributes":
                for field in ("owner", "range"):
                    if existing.get(field) and candidate.get(field) and existing[field] != candidate[field]:
                        conflicts.append({"section": section, "ref": name, "field": field,
                                          "current": existing[field], "draft": candidate[field]})
                out[section][name] = {**candidate, **existing}
            else:
                merged = {**candidate, **existing}
                merged["properties"] = unique((existing.get("properties") or []) +
                                               (candidate.get("properties") or []))
                merged["capabilities"] = unique((existing.get("capabilities") or []) +
                                                (candidate.get("capabilities") or []))
                out[section][name] = merged
    out["schema_version"] = version
    out.setdefault("constraints", {}).setdefault("max_hops", 3)
    return {"schema": out, "conflicts": conflicts}

VERSION_MAX = 120          # ontology.* 表中 schema_version / ontology_version 列为 VARCHAR(120)


def next_version(current: str | None, job: dict) -> str:
    """生成下一个版本号：只保留基础版本（第一个 + 之前的部分），补丁号 +1，再附本次来源。

    旧逻辑把每次发布的 +来源 后缀不断拼接（v2.0.0+hr.a9334e+upload.f17740+...），
    十几次后超出 VARCHAR(120) 导致发布报 500。新格式形如 v2.0.13+dm_staff.1e66d2。
    """
    import re as _re
    base = (current or "v0.0.0").split("+", 1)[0]
    n_plus = (current or "").count("+")                       # 旧格式里已累计的发布次数
    m = _re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", base)
    if m:
        major, minor, patch = (int(x) for x in m.groups())
        base = f"v{major}.{minor}.{patch + max(1, n_plus)}"
    src = _re.sub(r"[^A-Za-z0-9_\-]+", "_", str(job.get("source") or "src"))[:40]
    return f"{base}+{src}.{str(job['id'])[:6]}"[:VERSION_MAX]


def publish_schema(job: dict, actor: str, version: str | None = None, tenant: str = "default") -> dict:
    from app.modules.ontology import cq_v2
    from app.modules.ontology import outbox

    eng = cq_v2.get_engine()
    reg = eng.registry
    ver = version or next_version(reg.current_version(), job)
    merged = merge_schema(reg.current(), job["draft"]["schema"], ver)
    if merged["conflicts"]:
        return {"published": False, "conflicts": merged["conflicts"],
                "reason": "关系端点与已发布版本冲突，需人工裁决（改名或走破坏性变更）"}
    res = eng.registry_publish(merged["schema"], actor=actor)      # 落库 + 版本轨
    try:                                                            # 运行时关系目录立即跟随新版本
        from app.modules.ontology import type_registry
        res["runtime_catalog"] = type_registry.install(reg)
    except Exception as exc:  # noqa: BLE001
        res["runtime_catalog"] = {"installed": False, "error": str(exc)[:200]}
    try:
        outbox.record("ontology.published", job["id"], {
            "schema_version": res["schema_version"], "job_id": job["id"],
            "compatibility": res.get("compatibility")}, aggregate_type="Ontology",
            tenant_id=tenant, ontology_version=res["schema_version"])
    except Exception as exc:  # noqa: BLE001  Schema 已提交但事件未登记，必须显式报告部分成功
        res["event"] = {"recorded": False, "error": str(exc)[:200]}
    runtime_ok = (res.get("runtime_catalog") or {}).get("installed") is True
    event_ok = res.get("event") != "outbox_record_failed" and not (
        isinstance(res.get("event"), dict) and res["event"].get("recorded") is False)
    if not runtime_ok or not event_ok:
        return {**res, "published": False, "partial_commit": True,
                "schema_version": res.get("schema_version"),
                "reason": "Schema 已提交，但运行时安装或发布事件登记失败；需执行恢复流程"}
    return {"published": True, **res}


def recover_partial_publish(job: dict, actor: str, tenant: str,
                            ontology_version: str) -> dict:
    """在不重复创建 Schema 版本的前提下，恢复运行时目录、Outbox 和指标注册。"""
    from app.modules.ontology import cq_v2, outbox, type_registry
    from app.db import session as db

    engine = cq_v2.get_engine()
    registry = engine.registry
    if registry.current_version() != ontology_version:
        return {"published": False, "partial_commit": True,
                "reason": "当前 Registry 版本已变化，不能自动恢复旧任务；需人工核对版本"}
    runtime = type_registry.install(registry)
    if runtime.get("installed") is not True:
        return {"published": False, "partial_commit": True,
                "reason": "运行时类型目录恢复失败", "runtime_catalog": runtime}
    event = db.fetch_one(
        "SELECT id FROM ingest.outbox_events WHERE event_type='ontology.published' "
        "AND aggregate_id=:a AND tenant_id=:t AND payload->>'job_id'=:j LIMIT 1",
        {"a": job["id"], "t": tenant, "j": job["id"]})
    if not event:
        outbox.record("ontology.published", job["id"], {
            "schema_version": ontology_version, "job_id": job["id"]},
            aggregate_type="Ontology", tenant_id=tenant, ontology_version=ontology_version)
    metrics = publish_metrics(job.get("metrics") or {"metrics": {}},
                              job["draft"]["schema"], tenant, actor, ontology_version)
    if metrics.get("validation", {}).get("valid") is False:
        return {"published": False, "partial_commit": True,
                "reason": "Schema 和运行时已恢复，但指标校验仍未通过",
                "runtime_catalog": runtime, "metrics": metrics}
    return {"published": True, "schema_version": ontology_version,
            "runtime_catalog": runtime, "event": "recorded", "metrics": metrics,
            "recovered": True}


def to_engine_definitions(metric_set: dict, schema: dict, ontology_version: str | None = None) -> list[dict]:
    """automodel 指标 → metric_engine 定义。derived(时间粒度/同环比) 以 time_grain/transform 字段携带。"""
    out = []
    for mid, m in metric_set["metrics"].items():
        # 现有 metric_engine.preview 只覆盖 count/sum/avg/max/distinct 与四则复合；
        # 时间粒度、同环比、非空计数、多对多关联计数、占比需要 automodel 执行器（/automodel/.../evaluate）
        needs_automodel = (m["kind"] == "derived" or m.get("agg") in ("count_nonnull", "count_links")
                           or "total(" in (m.get("expr") or ""))
        base = {"metric_id": mid, "name": m["label"], "unit": m.get("unit") or "",
                "executor": "automodel" if needs_automodel else "metric_engine",
                "dimensions": m.get("dims_allowed") or [], "status": "draft",
                "ontology_version": ontology_version,
                "evidence": {"source": "automodel", "reason": m.get("reason")}}
        if m["kind"] == "atomic":
            out.append({**base, "kind": "native", "subject": m["cls"],
                        "aggregation": AGG_MAP[m["agg"]],
                        "field": m.get("measure") or m.get("relation") or "",
                        "filter": "nonnull" if m["agg"] == "count_nonnull" else None,
                        "relation": m.get("relation"), "time_grain": None,
                        "dependencies": [], "formula": ""})
        elif m["kind"] == "derived":
            out.append({**base, "kind": "derived", "subject": m.get("cls"),
                        "formula": m["base"], "dependencies": [m["base"]],
                        "time_grain": m.get("grain"), "time_field": m.get("time"),
                        "transform": m.get("transform"), "periods": m.get("periods")})
        else:
            expr = m["expr"].replace("total(", "(")          # 占比：分母总量由执行器按维度汇总
            out.append({**base, "kind": "composite", "subject": m.get("frame"),
                        "formula": expr, "dependencies": m["deps"],
                        "share_of_total": "total(" in m["expr"], "time_grain": None})
    return out


def publish_metrics(metric_set: dict, schema: dict, tenant: str, actor: str,
                    ontology_version: str | None = None) -> dict:
    from app.api.v1 import ontology_metrics as api
    from app.db import session as db
    from app.modules.ontology.runtime import metric_engine as ME

    defs = to_engine_definitions(metric_set, schema, ontology_version)
    check = ME.validate([dict(d) for d in defs])
    if not check.get("valid"):
        return {"saved": 0, "validation": check}
    api.ensure_schema()
    saved = 0
    for d in defs:
        row = db.fetch_one(
            "SELECT COALESCE(max(version), 0) AS v, "
            " (array_agg(checksum ORDER BY version DESC))[1] AS c "
            "FROM ontology.metric_definitions WHERE tenant_id=:t AND metric_id=:m",
            {"t": tenant, "m": d["metric_id"]})
        cs = ME.checksum(d)
        if row and row.get("c") == cs:
            continue                                         # 定义未变，不重复建版本
        db.execute(
            "INSERT INTO ontology.metric_definitions (tenant_id, metric_id, name, version, status, "
            " definition, checksum, ontology_version, created_by) VALUES "
            "(:t, :m, :n, :v, 'draft', CAST(:d AS jsonb), :c, :ov, :a)",
            {"t": tenant, "m": d["metric_id"], "n": d["name"], "v": int((row or {}).get("v") or 0) + 1,
             "d": json.dumps(d, ensure_ascii=False), "c": cs, "ov": (ontology_version or "")[:VERSION_MAX] or None, "a": actor})
        saved += 1
    return {"saved": saved, "total": len(defs), "validation": check,
            "next": "在指标页审批后调用 /ontology/metrics/{metric_id}/publish 发布"}
