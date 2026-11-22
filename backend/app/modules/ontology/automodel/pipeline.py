"""编排：profile → draft → map → validate → review → preview → publish gate。"""
from __future__ import annotations

import copy
import time
import uuid

from . import business_object_discovery, generator, mapper, materializer, profiler, release_gate
from .connectors import TableSet

STAGES = ("profile", "business_object_discovery", "draft", "map", "validate", "materialize", "metrics")


def run(ts: TableSet, existing_schema: dict | None = None, llm=None,
        version: str | None = None, stop_after: str = "materialize") -> dict:
    job = {"id": str(uuid.uuid4()), "source": ts.source_id, "source_kind": ts.kind,
           "stages": {}, "started_at": time.time(), "status": "running"}

    def stage(name, fn):
        t0 = time.perf_counter()
        out = fn()
        job["stages"][name] = {"ms": round((time.perf_counter() - t0) * 1000), "ok": True}
        return out

    profiles = stage("profile", lambda: profiler.profile(ts))
    job["profiles"] = {k: v.to_dict() for k, v in profiles.items()}
    if stop_after == "profile":
        return _done(job)
    discovery = stage("business_object_discovery", lambda: business_object_discovery.discover(
        profiles, existing_schema=existing_schema))
    job["business_object_discovery"] = discovery
    if stop_after == "business_object_discovery":
        return _done(job)
    draft = stage("draft", lambda: generator.generate(
        profiles, version or f"auto-{ts.source_id}-{job['id']}", existing_schema, llm,
        source_id=ts.source_id, discovery=discovery))
    draft["business_object_discovery"] = discovery
    job["draft"] = draft
    if stop_after == "draft":
        return _done(job)
    contract = stage("map", lambda: mapper.build_contract(ts.source_id, draft, profiles))
    job["contract"] = contract
    issues = stage("validate", lambda: mapper.validate_contract(contract, draft["schema"], profiles))
    from app.modules.ontology.semantic.schema_def import validate_business_model
    business_issues = stage("business_model_validate",
                            lambda: validate_business_model(draft["schema"]))
    job["business_model_issues"] = business_issues
    issues.extend({
        "level": "error", "code": item["code"],
        "table": item.get("ref"), "msg": item["message"],
    } for item in business_issues)
    from app.modules.ontology.semantic.standards import check_ontology_modeling_standards
    standards_report = stage("modeling_standards",
                             lambda: check_ontology_modeling_standards(draft["schema"]))
    job["standards_report"] = standards_report
    issues.extend({
        "level": "error" if item["severity"] == "error" else "warning",
        "code": item["code"], "table": item.get("ref"),
        "msg": item["message"], "recommendation": item["recommendation"],
    } for item in standards_report["findings"])
    job["issues"] = issues
    has_blocking = any(i["level"] == "error" for i in issues)
    has_warnings = any(i["level"] == "warning" for i in issues)
    if has_blocking or stop_after in ("map", "validate"):
        job["status"] = "needs_review" if (issues or draft.get("review")) else "validated"
        job["release_gate"] = release_gate.evaluate(job)
        return _done(job, keep_status=True)
    job["materialized"] = stage("materialize", lambda: materializer.materialize(ts, contract))
    from . import metrics as mx
    job["metrics"] = stage("metrics", lambda: mx.generate(draft["schema"]))
    job["status"] = "needs_review" if (draft["review"] or has_warnings) else "ready_to_publish"
    job["release_gate"] = release_gate.evaluate(job)
    return _done(job, keep_status=True)


def _done(job, keep_status=False):
    if not keep_status:
        job["status"] = "partial"
    job["finished_at"] = time.time()
    return job


def _audit(job: dict, action: str, actor: str, **details) -> dict:
    event = {"change_id": str(uuid.uuid4()), "action": action, "actor": actor,
             "reviewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "schema_version": (job.get("draft") or {}).get("schema", {}).get("schema_version"),
             **details}
    job.setdefault("change_set", []).append(event)
    job.setdefault("actions_log", []).append(event.copy())
    return event


def _rename_property_refs(schema: dict, old: str, new: str) -> None:
    attrs = schema.setdefault("attributes", {})
    if old in attrs:
        attrs[new] = attrs.pop(old)
    for obj in (schema.get("entity_types") or {}).values():
        for item in obj.get("attributes") or []:
            if item.get("name") == old:
                item["name"] = new
        if obj.get("title_property") == old:
            obj["title_property"] = new
        for rule in obj.get("identity_rules") or []:
            rule["key_properties"] = [new if x == old else x for x in rule.get("key_properties", [])]
        for binding in obj.get("backing_sources") or []:
            binding["property_mappings"] = {
                col: (new if prop == old else prop)
                for col, prop in (binding.get("property_mappings") or {}).items()
            }
    interfaces = schema.get("interfaces") or {}
    values = interfaces.values() if isinstance(interfaces, dict) else interfaces
    for interface in values:
        interface["properties"] = [new if x == old else x for x in interface.get("properties", [])]
    for relation in (schema.get("relation_types") or {}).values():
        for item in relation.get("properties") or []:
            if item.get("name") == old:
                item["name"] = new


def _remove_property_refs(schema: dict, name: str) -> None:
    schema.setdefault("attributes", {}).pop(name, None)
    for obj in (schema.get("entity_types") or {}).values():
        obj["attributes"] = [x for x in (obj.get("attributes") or []) if x.get("name") != name]
        if obj.get("title_property") == name:
            obj["title_property"] = ""
        obj["identity_rules"] = [
            {**rule, "key_properties": [x for x in rule.get("key_properties", []) if x != name]}
            for rule in (obj.get("identity_rules") or [])
        ]
        obj["identity_rules"] = [x for x in obj["identity_rules"] if x["key_properties"]]
        for binding in obj.get("backing_sources") or []:
            binding["property_mappings"] = {
                col: prop for col, prop in (binding.get("property_mappings") or {}).items()
                if prop != name
            }
    interfaces = schema.get("interfaces") or {}
    values = interfaces.values() if isinstance(interfaces, dict) else interfaces
    for interface in values:
        interface["properties"] = [x for x in interface.get("properties", []) if x != name]
    for relation in (schema.get("relation_types") or {}).values():
        relation["properties"] = [x for x in (relation.get("properties") or []) if x.get("name") != name]


def _rename_object_refs(schema: dict, old: str, new: str) -> None:
    for obj in (schema.get("entity_types") or {}).values():
        if obj.get("parent") == old:
            obj["parent"] = new
    for relation in (schema.get("relation_types") or {}).values():
        for key in ("source", "target"):
            if relation.get(key) == old:
                relation[key] = new
        for key in ("source_types", "target_types"):
            if key in relation:
                relation[key] = [new if x == old else x for x in relation[key]]
    for attr in (schema.get("attributes") or {}).values():
        if attr.get("owner") == old:
            attr["owner"] = new


def apply_review(job: dict, decisions: list[dict], actor: str = "reviewer",
                 acknowledge_warnings: bool = False) -> dict:
    """审核变更统一同步 Schema 引用；所有操作保留前后快照与审计。"""
    schema = job["draft"]["schema"]
    sections = {"class": "entity_types", "relation": "relation_types", "attribute": "attributes"}
    allowed = {"accept", "reject", "rename", "retarget", "merge"}
    for decision in decisions or []:
        kind, ref, action = decision.get("kind"), decision.get("ref"), decision.get("action")
        if kind not in sections:
            raise ValueError(f"不支持的审核对象类型：{kind}")
        if action not in allowed:
            raise ValueError(f"不支持的审核动作：{action}")
        section = schema[sections[kind]]
        if ref not in section:
            raise ValueError(f"待审核对象不存在：{kind}/{ref}")
        before = copy.deepcopy(section[ref])
        if decision.get("label"):
            section[ref]["label"] = decision["label"]
        ref_after = ref
        if action == "reject":
            section.pop(ref)
            if kind == "class":
                job["draft"]["class_map"] = {t: c for t, c in job["draft"]["class_map"].items() if c != ref}
                for key in [k for k, v in schema["relation_types"].items()
                            if ref in (v.get("source"), v.get("target"))
                            or ref in (v.get("source_types") or []) or ref in (v.get("target_types") or [])]:
                    schema["relation_types"].pop(key)
                for key in [k for k, v in schema["attributes"].items() if v.get("owner") == ref]:
                    _remove_property_refs(schema, key)
                for obj in (schema.get("entity_types") or {}).values():
                    if obj.get("parent") == ref:
                        obj["parent"] = ""
            elif kind == "attribute":
                _remove_property_refs(schema, ref)
        elif action == "rename":
            new_name = decision.get("new_name")
            if not isinstance(new_name, str) or not new_name.strip() or new_name in section:
                raise ValueError("rename 必须提供非空且未占用的 new_name")
            new_name = new_name.strip()
            section[new_name] = section.pop(ref)
            ref_after = new_name
            if kind == "class":
                job["draft"]["class_map"] = {t: (new_name if c == ref else c)
                                             for t, c in job["draft"]["class_map"].items()}
                _rename_object_refs(schema, ref, new_name)
            elif kind == "attribute":
                _rename_property_refs(schema, ref, new_name)
        elif action == "merge":
            if kind != "class":
                raise ValueError("merge 当前仅支持业务对象类型")
            target = decision.get("target")
            if not isinstance(target, str) or target not in schema["entity_types"] or target == ref:
                raise ValueError("merge 必须指定一个已存在且不同的 target 对象类型")
            source_obj, target_obj = section[ref], section[target]
            if (source_obj.get("object_kind") and target_obj.get("object_kind")
                    and source_obj["object_kind"] != target_obj["object_kind"]):
                raise ValueError("对象类别不一致，不能自动归并")
            target_obj["attributes"] = list({x.get("name"): x for x in
                (target_obj.get("attributes") or []) + (source_obj.get("attributes") or [])}.values())
            for field in ("identity_rules", "backing_sources", "interfaces", "aliases"):
                target_obj[field] = list({str(x): x for x in
                    (target_obj.get(field) or []) + (source_obj.get(field) or [])}.values())
            if not target_obj.get("title_property"):
                target_obj["title_property"] = source_obj.get("title_property", "")
            if not target_obj.get("description"):
                target_obj["description"] = source_obj.get("description", "")
            job["draft"]["class_map"] = {t: (target if c == ref else c)
                                         for t, c in job["draft"]["class_map"].items()}
            _rename_object_refs(schema, ref, target)
            for attr in (schema.get("attributes") or {}).values():
                if attr.get("owner") == ref:
                    attr["owner"] = target
            section.pop(ref)
            ref_after = target
        elif action == "retarget":
            if kind != "relation":
                raise ValueError("retarget 当前仅支持关系类型")
            new_source = decision.get("new_source", before.get("source"))
            new_target = decision.get("new_target", before.get("target"))
            if new_source not in schema["entity_types"] or new_target not in schema["entity_types"]:
                raise ValueError("关系 retarget 的 source/target 必须是已定义对象类型")
            section[ref]["source"], section[ref]["target"] = new_source, new_target
        after = copy.deepcopy(section.get(ref_after)) if action != "reject" else None
        _audit(job, action, actor, target_type=kind, target_ref=ref,
               target_ref_after=ref_after, before=before, after=after,
               evidence=decision.get("evidence"), comment=decision.get("comment"))
        inferences = job["draft"].get("inferences") or []
        job["draft"]["inferences"] = [
            item for item in inferences if not (item.get("kind") == kind and item.get("ref") == ref)]
        if action not in ("reject",):
            job["draft"]["inferences"].append({
                "kind": kind, "ref": ref_after, "confidence": 1.0,
                "reason": f"人工审核：{action}", "reviewed_by": actor})
    job["draft"]["review"] = [i for i in job["draft"].get("inferences", []) if i.get("confidence", 0) < 0.8]
    if acknowledge_warnings:
        job["standards_review_acknowledged"] = True
        _audit(job, "acknowledge_standards_warnings", actor,
               finding_codes=[x.get("code") for x in (job.get("standards_report") or {}).get("findings", [])
                              if x.get("severity") == "warning"])
    job["release_gate"] = release_gate.evaluate(job)
    return job


def acknowledge_standards_warnings(job: dict, actor: str = "reviewer") -> dict:
    """审核人确认当前规范报告中的警告；重建后必须重新确认。"""
    warnings = [x for x in (job.get("standards_report") or {}).get("findings", [])
                if x.get("severity") == "warning"]
    job["standards_review_acknowledged"] = True
    _audit(job, "acknowledge_standards_warnings", actor,
           finding_codes=[x.get("code") for x in warnings])
    job["release_gate"] = release_gate.evaluate(job)
    return job

def rebuild(job: dict, ts: TableSet) -> dict:
    """审核后重建契约、执行源字段验证、预览物化和指标生成。"""
    from . import metrics as mx
    profiles = profiler.profile(ts)
    job["profiles"] = {k: v.to_dict() for k, v in profiles.items()}
    job["contract"] = mapper.build_contract(ts.source_id, job["draft"], profiles)
    job["issues"] = mapper.validate_contract(job["contract"], job["draft"]["schema"], profiles)
    from app.modules.ontology.semantic.schema_def import validate_business_model
    job["business_model_issues"] = validate_business_model(job["draft"]["schema"])
    job["issues"].extend({
        "level": "error", "code": item["code"],
        "table": item.get("ref"), "msg": item["message"],
    } for item in job["business_model_issues"])
    from app.modules.ontology.semantic.standards import check_ontology_modeling_standards
    job["standards_report"] = check_ontology_modeling_standards(job["draft"]["schema"])
    job["standards_review_acknowledged"] = False
    job["issues"].extend({
        "level": "error" if item["severity"] == "error" else "warning",
        "code": item["code"], "table": item.get("ref"),
        "msg": item["message"], "recommendation": item["recommendation"],
    } for item in job["standards_report"]["findings"])
    if any(i["level"] == "error" for i in job["issues"]):
        job["status"] = "needs_review"
        job["release_gate"] = release_gate.evaluate(job)
        return job
    job["materialized"] = materializer.materialize(ts, job["contract"])
    job["metrics"] = mx.generate(job["draft"]["schema"])
    job["status"] = "needs_review" if (job["draft"]["review"] or any(i["level"] == "warning" for i in job["issues"])) else "ready_to_publish"
    job["release_gate"] = release_gate.evaluate(job)
    return job


def approve_and_publish(job: dict, actor: str, tenant: str = "default",
                        version: str | None = None) -> dict:
    """发布前强制执行统一门禁；不允许仅凭 review 列表为空就发布。"""
    from . import publish
    gate = release_gate.assert_publishable(
        job, require_cq=bool((job.get("config") or {}).get("require_cq_for_publish")))
    res = publish.publish_schema(job, actor, version)
    if not res.get("published"):
        return res
    runtime = res.get("runtime_catalog") or {}
    if runtime.get("installed") is not True:
        job["status"] = "publish_partial_failure"
        job["publish_error"] = "Schema 已提交，但运行时类型目录安装失败；禁止标记为 published"
        return {**res, "published": False, "partial_commit": True,
                "reason": job["publish_error"]}
    job["schema_version"] = res["schema_version"]
    job["published_by"] = actor
    try:
        res["metrics"] = publish.publish_metrics(job.get("metrics") or {"metrics": {}},
                                                  job["draft"]["schema"], tenant, actor,
                                                  res["schema_version"])
    except Exception as exc:  # noqa: BLE001  Schema 已提交，指标失败必须作为部分成功暴露
        job["status"] = "publish_partial_failure"
        job["publish_error"] = f"Schema 已提交，但指标注册异常：{type(exc).__name__}"
        return {**res, "published": False, "partial_commit": True,
                "reason": job["publish_error"]}
    metric_result = res["metrics"] or {}
    if metric_result.get("validation", {}).get("valid") is False:
        job["status"] = "publish_partial_failure"
        job["publish_error"] = "Schema 已提交，但指标注册校验失败"
        return {**res, "published": False, "partial_commit": True,
                "reason": job["publish_error"]}
    job["status"] = "published"
    job["release_gate"] = gate
    _audit(job, "publish", actor, schema_version=res["schema_version"])
    return res


def write(job: dict, db, visibility: str = "owner") -> dict:
    """Discovery 物化样本不得写入正式库；正式写入使用已审核 mapping_execution API。"""
    raise ValueError(
        "Automodel Discovery 结果仅用于预览；正式数据写入请调用 ontology.mapping_execution，"
        "并通过 IngestionRun 分批执行。"
    )
