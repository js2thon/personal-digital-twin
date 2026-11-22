"""编排：profile → draft → map → validate → review → preview → publish gate。"""
from __future__ import annotations

import copy
import time
import uuid

from . import generator, mapper, materializer, profiler, release_gate
from .connectors import TableSet

STAGES = ("profile", "draft", "map", "validate", "materialize", "metrics")


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
    draft = stage("draft", lambda: generator.generate(
        profiles, version or f"auto-{ts.source_id}-{job['id']}", existing_schema, llm,
        source_id=ts.source_id))
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
    job["issues"] = issues
    if any(i["level"] == "error" for i in issues) or stop_after in ("map", "validate"):
        job["status"] = "needs_review" if issues else "validated"
        job["release_gate"] = release_gate.evaluate(job)
        return _done(job, keep_status=True)
    job["materialized"] = stage("materialize", lambda: materializer.materialize(ts, contract))
    from . import metrics as mx
    job["metrics"] = stage("metrics", lambda: mx.generate(draft["schema"]))
    job["status"] = "needs_review" if draft["review"] else "ready_to_publish"
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


def apply_review(job: dict, decisions: list[dict], actor: str = "reviewer") -> dict:
    """审核候选：accept/reject/rename/retarget；严格校验动作并记录不可丢失的变更审计。"""
    schema = job["draft"]["schema"]
    sections = {"class": "entity_types", "relation": "relation_types", "attribute": "attributes"}
    allowed = {"accept", "reject", "rename", "retarget"}
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
        if action == "reject":
            section.pop(ref)
            if kind == "class":
                job["draft"]["class_map"] = {t: c for t, c in job["draft"]["class_map"].items() if c != ref}
                for key in [k for k, v in schema["relation_types"].items()
                            if ref in (v.get("source"), v.get("target"))]:
                    schema["relation_types"].pop(key)
                for key in [k for k, v in schema["attributes"].items() if v.get("owner") == ref]:
                    schema["attributes"].pop(key)
        elif action == "rename":
            new_name = decision.get("new_name")
            if not isinstance(new_name, str) or not new_name.strip() or new_name in section:
                raise ValueError("rename 必须提供非空且未占用的 new_name")
            new_name = new_name.strip()
            section[new_name] = section.pop(ref)
            if kind == "class":
                job["draft"]["class_map"] = {t: (new_name if c == ref else c)
                                             for t, c in job["draft"]["class_map"].items()}
                for relation in schema["relation_types"].values():
                    relation["source"] = new_name if relation.get("source") == ref else relation.get("source")
                    relation["target"] = new_name if relation.get("target") == ref else relation.get("target")
                for attr in schema["attributes"].values():
                    attr["owner"] = new_name if attr.get("owner") == ref else attr.get("owner")
            ref_after = new_name
        elif action == "retarget":
            if kind != "relation":
                raise ValueError("retarget 当前仅支持关系类型")
            new_source = decision.get("new_source", before.get("source"))
            new_target = decision.get("new_target", before.get("target"))
            if new_source not in schema["entity_types"] or new_target not in schema["entity_types"]:
                raise ValueError("关系 retarget 的 source/target 必须是已定义对象类型")
            section[ref]["source"], section[ref]["target"] = new_source, new_target
        ref_after = decision.get("new_name") if action == "rename" else ref
        after = copy.deepcopy(section.get(ref_after)) if action != "reject" else None
        _audit(job, action, actor, target_type=kind, target_ref=ref,
               target_ref_after=ref_after, before=before, after=after,
               evidence=decision.get("evidence"), comment=decision.get("comment"))
        inferences = job["draft"].get("inferences") or []
        job["draft"]["inferences"] = [
            item for item in inferences if not (item.get("kind") == kind and item.get("ref") == ref)]
        if action != "reject":
            job["draft"]["inferences"].append({
                "kind": kind, "ref": ref_after, "confidence": 1.0,
                "reason": f"人工审核：{action}", "reviewed_by": actor})
    job["draft"]["review"] = [i for i in job["draft"].get("inferences", []) if i.get("confidence", 0) < 0.8]
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
    if any(i["level"] == "error" for i in job["issues"]):
        job["status"] = "needs_review"
        job["release_gate"] = release_gate.evaluate(job)
        return job
    job["materialized"] = materializer.materialize(ts, job["contract"])
    job["metrics"] = mx.generate(job["draft"]["schema"])
    job["status"] = "needs_review" if job["draft"]["review"] else "ready_to_publish"
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
    job["schema_version"] = res["schema_version"]
    job["published_by"] = actor
    res["metrics"] = publish.publish_metrics(job.get("metrics") or {"metrics": {}},
                                              job["draft"]["schema"], tenant, actor,
                                              res["schema_version"])
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
