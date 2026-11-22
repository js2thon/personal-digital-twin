"""编排：profile → draft → map → validate →（审核）→ materialize。

每阶段产物进 artifact，可持久化到 ontology.modelling_jobs 断点续跑。
发布 Schema 与写库是两个显式动作（approve_and_publish / write），默认不自动执行。
"""
from __future__ import annotations

import time
import uuid

from . import generator, mapper, materializer, profiler
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
        profiles, version or f"auto-{ts.source_id}-{job['id']}", existing_schema, llm))
    job["draft"] = draft
    if stop_after == "draft":
        return _done(job)
    contract = stage("map", lambda: mapper.build_contract(ts.source_id, draft, profiles))
    job["contract"] = contract
    issues = stage("validate", lambda: mapper.validate_contract(contract, draft["schema"]))
    job["issues"] = issues
    if any(i["level"] == "error" for i in issues) or stop_after in ("map", "validate"):
        job["status"] = "needs_review" if issues else "validated"
        return _done(job, keep_status=True)
    job["materialized"] = stage("materialize", lambda: materializer.materialize(ts, contract))
    from . import metrics as mx
    job["metrics"] = stage("metrics", lambda: mx.generate(draft["schema"]))
    job["status"] = "needs_review" if draft["review"] else "ready_to_publish"
    return _done(job, keep_status=True)


def _done(job, keep_status=False):
    if not keep_status:
        job["status"] = "partial"
    job["finished_at"] = time.time()
    return job


def apply_review(job: dict, decisions: list[dict]) -> dict:
    """人工审核：逐条 accept / reject / rename / retarget，然后重建契约并复校验。

    decisions: [{"kind": "class|relation|attribute", "ref": "...", "action": "accept|reject|rename",
                 "new_name": "...", "label": "..."}]
    """
    schema = job["draft"]["schema"]
    sec = {"class": "entity_types", "relation": "relation_types", "attribute": "attributes"}
    for d in decisions or []:
        s = schema[sec[d["kind"]]]
        ref = d["ref"]
        if ref not in s:
            continue
        if d.get("label"):
            s[ref]["label"] = d["label"]
        if d["action"] == "reject":
            s.pop(ref)
            if d["kind"] == "class":
                job["draft"]["class_map"] = {t: c for t, c in job["draft"]["class_map"].items() if c != ref}
                for k in [k for k, v in schema["relation_types"].items() if ref in (v["source"], v["target"])]:
                    schema["relation_types"].pop(k)
                for k in [k for k, v in schema["attributes"].items() if v["owner"] == ref]:
                    schema["attributes"].pop(k)
        elif d["action"] == "rename" and d.get("new_name") and d["new_name"] not in s:
            s[d["new_name"]] = s.pop(ref)
            if d["kind"] == "class":
                new = d["new_name"]
                job["draft"]["class_map"] = {t: (new if c == ref else c) for t, c in job["draft"]["class_map"].items()}
                for v in schema["relation_types"].values():
                    v["source"] = new if v["source"] == ref else v["source"]
                    v["target"] = new if v["target"] == ref else v["target"]
                for v in schema["attributes"].values():
                    v["owner"] = new if v["owner"] == ref else v["owner"]
        kept = [i for i in job["draft"]["inferences"] if not (i["kind"] == d["kind"] and i["ref"] == ref)]
        if d["action"] != "reject":
            kept.append({"kind": d["kind"], "ref": d.get("new_name") or ref, "confidence": 1.0,
                         "reason": f"人工审核：{d['action']}"})
        job["draft"]["inferences"] = kept
    job["draft"]["review"] = [i for i in job["draft"]["inferences"] if i["confidence"] < 0.8]
    return job


def rebuild(job: dict, ts: TableSet) -> dict:
    """审核后重建：契约 → 校验 → 物化 → 指标（画像与草案沿用已审核版本）。"""
    from . import metrics as mx
    profiles = profiler.profile(ts)
    job["contract"] = mapper.build_contract(ts.source_id, job["draft"], profiles)
    job["issues"] = mapper.validate_contract(job["contract"], job["draft"]["schema"])
    if any(i["level"] == "error" for i in job["issues"]):
        job["status"] = "needs_review"
        return job
    job["materialized"] = materializer.materialize(ts, job["contract"])
    job["metrics"] = mx.generate(job["draft"]["schema"])
    job["status"] = "needs_review" if job["draft"]["review"] else "ready_to_publish"
    return job


def approve_and_publish(job: dict, actor: str, tenant: str = "default",
                        version: str | None = None) -> dict:
    """人工审核通过后：发布 Schema（cq_v2 Registry）+ 指标草稿入库。"""
    from . import publish
    if job["draft"]["review"]:
        raise ValueError(f"仍有 {len(job['draft']['review'])} 项待审核")
    res = publish.publish_schema(job, actor, version)
    if not res.get("published"):
        return res
    job["schema_version"] = res["schema_version"]
    job["published_by"] = actor
    res["metrics"] = publish.publish_metrics(job["metrics"], job["draft"]["schema"], tenant, actor,
                                             res["schema_version"])
    job["status"] = "published"
    return res


def write(job: dict, db, visibility: str = "owner") -> dict:
    """写入实体/事实。必须在 approve_and_publish 之后（facts 依赖已发布的关系类型）。"""
    if job.get("status") != "published":
        raise ValueError("请先审核并发布本体")
    res = materializer.PgWriter(db).write(job["materialized"], visibility,
                                          ontology_version=job.get("schema_version"))
    job["written"] = res
    job["status"] = "written"
    return res
