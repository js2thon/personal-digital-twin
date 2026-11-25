"""通用服务端请求上下文构造；与问答、评测执行器解耦。"""
from __future__ import annotations

import hashlib
import json


_FORBIDDEN_BODY_KEYS = (
    "tenant_id", "user_id", "actor", "role", "scope", "space_id",
    "ontology_version", "schema_version", "policy", "permission_context", "principal",
)


def build_server_context(principal: dict | None, semantic_context: dict | None,
                         *, schema: dict | None = None,
                         task_snapshot_id: str | None = None,
                         body: dict | None = None) -> dict:
    """从服务端可信来源构造上下文，并披露被剥离的请求体伪造字段。"""
    from app.modules.ontology.semantic import versioning

    p, sc = principal or {}, semantic_context or {}
    stripped = [key for key in _FORBIDDEN_BODY_KEYS if key in (body or {})]
    current = versioning.current_version()
    ontology_version, schema_version = current["ontology_version"], current["schema_version"]
    bound = False
    if task_snapshot_id:
        try:
            frozen = versioning.resolve(task_snapshot_id, str(p.get("tenant_id") or "default"))
            ontology_version = frozen["ontology_version"]
            schema_version = frozen["schema_version"]
            bound = True
        except LookupError:
            pass
    ctx = {
        "tenant_id": str(p.get("tenant_id") or "default"),
        "user_id": str(p.get("user_id") or ""),
        "actor": str(p.get("actor") or sc.get("actor") or "owner"),
        "role": str(p.get("role") or (sc.get("permission_context") or {}).get("role") or "owner"),
        "scope": list(p.get("scope") or (sc.get("permission_context") or {}).get("scope") or []),
        "space_id": str(sc.get("space_id") or p.get("space_id") or "default"),
        "ontology_version": ontology_version, "schema_version": schema_version,
        "schema": schema or {},
        "policy": {"engine": "app/policy.decide", "fail_closed": True, "data_space_filter": True},
        "task_snapshot_id": task_snapshot_id, "version_bound": bound,
        "source": "global_middleware",
    }
    return {
        "context": ctx, "stripped_body_keys": stripped, "body_trusted": False,
        "note": "上下文全部由服务端注入；" +
                ("已剥离请求体伪造字段 " + ", ".join(stripped) if stripped else "请求体未含越权字段"),
    }


def _fingerprint(core: dict) -> str:
    """稳定内容指纹：忽略字典插入顺序，不包含随机或时间字段。"""
    raw = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
