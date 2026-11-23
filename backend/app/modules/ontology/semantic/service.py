"""OntologyService：所有新写路径的统一入口（V1.3 语义层方案 · §十一）。

方案要求「所有新写路径通过统一的 OntologyService.write_instance /
write_relation，内部做约束检查」。本模块把四个阶段串进**单个事务**：
  约束强制（fail-closed）→ 落权威边 → 推理补全（衍生候选，单独标记）→
  规则触发（只产证据）→ 版本绑定 → 写入审计。
规则结果绝不改写 knowledge.facts / ontology.relations 等既有权威数据。
"""
from __future__ import annotations

import uuid

from . import store, versioning
from .constraints import (ConstraintViolation, assert_valid, validate_instance,
                          validate_relation)
from .reasoning import expand as reasoning_expand
from .rules import evaluate_for_write


def _current_schema(schema: dict | None) -> dict:
    return schema or store.load_semantic_schema() or _bootstrap_schema()


def _bootstrap_schema() -> dict:
    """库里尚无快照时，从 Registry 派生内存 schema（不落库，纯读）。"""
    from .schema_def import default_semantic_schema, to_schema_dict
    return to_schema_dict(default_semantic_schema())


def load_schema() -> dict:
    return _current_schema(None)


def resolve_types(instances: list[dict]) -> dict:
    return {str(i.get("id")): (i.get("type_name") or "") for i in instances}


class OntologyService:
    """统一写入口；principal 来自全局中间件，不信任请求体注入的身份。"""

    def __init__(self, schema: dict | None = None, *, tenant_id: str = "default",
                 actor: str = "system"):
        self.schema = _current_schema(schema)
        self.tenant_id = tenant_id
        self.actor = actor
        self.ov = versioning.current_version()["ontology_version"]

    # ------------------------------------------------------------ write_instance
    def write_instance(self, payload: dict) -> dict:
        from .state_machine import validate_instance_transitions

        type_name = payload.get("type_name")
        report = validate_instance(self.schema, payload)
        if report["blocking"]:
            self._audit("write_instance", type_name, "rejected", report["violations"])
            assert_valid(report, f"实例 {type_name}")
        # 规则（integrity/authorization）作为确定性约束，deny 即拒绝
        rule_hit = evaluate_for_write(self.schema, "write_instance", payload)
        blocking_rules = [v for v in rule_hit["violations"] if v["severity"] == "violation"]
        if blocking_rules:
            self._audit("write_instance", type_name, "rejected", blocking_rules)
            raise ConstraintViolation("实例写入被业务规则拒绝：" +
                                      "；".join(v["message"] for v in blocking_rules),
                                      {"violations": blocking_rules})

        # P0-1 状态机：更新模式（payload 带 id 且库里存在）从服务端取旧值校验 old→new，
        # 不接受客户端自报旧值；新建模式只校验新值合法性。
        existing = None
        iid_given = str(payload.get("id") or "").strip()
        if iid_given:
            existing = store.get_instance(iid_given, self.tenant_id)
        props = dict(payload.get("properties") or {})
        sm_report = validate_instance_transitions(
            self.schema, type_name, props,
            previous=(existing.get("properties") if existing else None))
        if sm_report["blocking"]:
            self._audit("write_instance", iid_given or type_name, "rejected", sm_report["violations"])
            raise ConstraintViolation("实例写入被状态机拒绝：" +
                                      "；".join(v["message"] for v in sm_report["violations"]),
                                      {"violations": sm_report["violations"],
                                       "state_machine": sm_report["checked"]})

        rec = dict(payload)
        rec.update({"tenant_id": self.tenant_id, "created_by": self.actor,
                    "ontology_version": self.ov, "properties": props})
        if existing:
            rec["id"] = iid_given
            store.update_instance(rec)
            created = False
            iid = iid_given
        else:
            iid = store.insert_instance(rec)
            created = True
        self._audit("write_instance", iid, "accepted", [])
        return {"id": iid, "type_name": type_name, "ontology_version": self.ov,
                "constraints": report, "applied_rules": rule_hit["applied_rules"],
                "state_machine": sm_report["checked"], "created": created}

    # ------------------------------------------------------------ write_relation
    def write_relation(self, payload: dict, *, auto_reasoning: bool = True) -> dict:
        """写入一条权威关系：类型约束（继承感知）+ 基数 + 规则 + 推理补全。"""
        src, dst = str(payload.get("source_id") or ""), str(payload.get("target_id") or "")
        # 必须按 ID 在当前租户内精确读取端点；禁止在大租户列表截断后
        # 回退信任客户端提交的 source_type / target_type。
        src_instance = store.get_instance(src, self.tenant_id) if src else None
        dst_instance = store.get_instance(dst, self.tenant_id) if dst else None
        if not src_instance or not dst_instance:
            violations = [{
                "constraint": "relation_endpoint",
                "severity": "violation",
                "message": "源或目标实例不存在，或不属于当前租户",
            }]
            self._audit("write_relation", payload.get("relation_type"), "rejected", violations)
            raise ConstraintViolation("关系写入被拒绝：源或目标实例不存在，或不属于当前租户",
                                      {"blocking": True, "violations": violations})
        types = resolve_types([src_instance, dst_instance])
        src_type = types.get(src, "")
        dst_type = types.get(dst, "")
        check = dict(payload, source_type=src_type, target_type=dst_type)
        relation_type = str(payload.get("relation_type") or "")
        relation_spec = (self.schema.get("relation_types") or {}).get(relation_type) or {}
        cardinality = relation_spec.get("cardinality", "many_to_many")
        existing = store.find_relation_cardinality_conflicts(
            self.tenant_id, relation_type, src, dst, cardinality
        )

        report = validate_relation(self.schema, check, existing_edges=existing)
        if report["blocking"]:
            self._audit("write_relation", payload.get("relation_type"),
                        "rejected", report["violations"])
            assert_valid(report, f"关系 {payload.get('relation_type')}")
        rule_hit = evaluate_for_write(self.schema, "write_relation", check)
        blocking_rules = [v for v in rule_hit["violations"] if v["severity"] == "violation"]
        if blocking_rules:
            self._audit("write_relation", payload.get("relation_type"),
                        "rejected", blocking_rules)
            raise ConstraintViolation("关系写入被业务规则拒绝：" +
                                      "；".join(v["message"] for v in blocking_rules),
                                      {"violations": blocking_rules})

        rec = dict(check)
        rec.update({"tenant_id": self.tenant_id, "created_by": self.actor,
                    "ontology_version": self.ov, "derived": False})
        rid = store.insert_relation(rec)

        # 推理补全：对称/互逆/传递都只作 derived 候选，不覆盖权威边
        derived: list[dict] = []
        if auto_reasoning:
            all_edges = existing + [dict(rec, id=rid)]
            exp = reasoning_expand(self.schema, all_edges)
            candidates = (exp["symmetric"] + exp["inverse"] + exp["transitive"])
            # 只落「与本次新边直接相关」的对称/互逆候选，传递闭包作为证据返回不落库
            for c in candidates:
                if c.get("derived_from") in (rid, src, dst) and \
                        c.get("derived_by") in ("symmetric", "inverse"):
                    c.update({"tenant_id": self.tenant_id, "created_by": self.actor,
                              "ontology_version": self.ov, "status": "candidate"})
                    store.insert_relation(c)
                    derived.append(c)
            transitive_preview = exp["transitive"][:20]
        else:
            transitive_preview = []

        self._audit("write_relation", rid, "accepted", [])
        return {
            "id": rid, "relation_type": payload.get("relation_type"),
            "source_type": src_type, "target_type": dst_type,
            "ontology_version": self.ov,
            "constraints": report, "applied_rules": rule_hit["applied_rules"],
            "derived_candidates": rule_hit["derived_candidates"],
            "derived_edges_written": derived,
            "transitive_preview": transitive_preview,
            "facts_written": False,
            "note": "推理产物为 derived 候选；未改写任何权威 Fact/关系",
        }

    # ------------------------------------------------------------ 违规标注（验收 6）
    def violations(self) -> dict:
        """读侧：给已落库权威边标注类型违规，供前端红色高亮。"""
        edges = store.list_relations(self.tenant_id, "derived = false", limit=2000)
        out: list[dict] = []
        for e in edges:
            rep = validate_relation(self.schema, e, existing_edges=edges)
            if rep["blocking"]:
                out.append({"id": e.get("id"), "relation_type": e.get("relation_type"),
                            "source_id": e.get("source_id"), "target_id": e.get("target_id"),
                            "source_type": e.get("source_type"),
                            "target_type": e.get("target_type"),
                            "violations": rep["violations"],
                            "severity": "violation"})
        return {"checked": len(edges), "violation_count": len(out),
                "violations": out,
                "constraint_mode": "application_enforced"}

    def _audit(self, operation: str, ref, outcome: str, violations: list) -> None:
        try:
            store.audit_write({"tenant_id": self.tenant_id, "operation": operation,
                               "target_ref": str(ref) if ref else None,
                               "outcome": outcome, "violations": violations,
                               "ontology_version": self.ov, "created_by": self.actor})
        except Exception:  # noqa: BLE001  审计失败不影响主写入判定
            pass


def ensure_ready() -> dict:
    """启动/维护入口：建表 → 播种语义 Schema 快照 → 播种核心回归集。

    版本口径对齐：schema 定义里的 ontology_version 统一取当前 published
    ontology_versions.version，避免「Registry 版本 vs 运行时版本」双轨漂移。
    """
    schema_report = store.ensure_schema()
    schema = _bootstrap_schema()
    published = versioning.current_version()["ontology_version"]
    schema["ontology_version"] = published
    store.upsert_semantic_schema(schema)
    from . import regression
    set_report = regression.ensure_core_set()
    return {"schema_ddl": schema_report,
            "schema_version": schema.get("schema_version"),
            "ontology_version": versioning.current_version()["ontology_version"],
            "entity_types": len(schema.get("entity_types") or {}),
            "relation_types": len(schema.get("relation_types") or {}),
            "business_rules": len(schema.get("business_rules") or []),
            "core_regression_set": (set_report or {}).get("set_id"),
            "durable": True, "store": "postgres"}
