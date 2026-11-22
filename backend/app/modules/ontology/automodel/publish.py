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
    """当前 Registry 版本 + 草案 → 新版本（只增不删，冲突项以当前版本为准并记录）。"""
    out = copy.deepcopy(current or {})
    conflicts = []
    for sec in ("entity_types", "relation_types", "attributes"):
        out.setdefault(sec, {})
        for k, v in (draft.get(sec) or {}).items():
            if k in out[sec]:
                cur = out[sec][k]
                if sec == "relation_types" and (cur.get("source"), cur.get("target")) != (v.get("source"), v.get("target")):
                    conflicts.append({"section": sec, "ref": k, "current": cur, "draft": v})
                    continue
                # 复用已有定义，只补充映射元数据
                out[sec][k] = {**v, **cur, "mappings": list({json.dumps(x, sort_keys=True): x for x in
                               (cur.get("mappings") or []) + [{kk: v.get(kk) for kk in
                               ("source_table", "table", "column", "via_table") if v.get(kk)}]}.values())}
            else:
                out[sec][k] = v
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


def publish_schema(job: dict, actor: str, version: str | None = None) -> dict:
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
            ontology_version=res["schema_version"])
    except Exception:  # noqa: BLE001  事件写入失败不回滚已发布版本，但要显式返回
        res["event"] = "outbox_record_failed"
    return {"published": True, **res}


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
