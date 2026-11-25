"""Ontology 语义层增强回归（V1.3 语义层方案 · Phase A/B/C）。

分两层，保证零库环境也能稳定通过：
* 纯逻辑层（默认全跑）：Schema-as-Code / 继承 / 对称 / 互逆 / 有限传递 /
  写入侧约束拒绝 / 业务规则 / JSON Schema 导出 / 指纹稳定性 / 幂等键构造；
* 实库层（db_required，skipif）：ensure_ready / 约束拒绝 400 / 冻结还原 /
  版本冻结 / 权限过滤 / 语义指标。

验收对照：
  类型约束拒绝 / 任务还原到 ontology_version / 图谱违规可视化数据。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.ontology.semantic import constraints as C
from app.modules.ontology.semantic import reasoning as R
from app.modules.ontology.semantic import rules as RU
from app.modules.ontology.semantic import schema_def as SD
from app.modules.ontology.semantic import service as SEM
from app.modules.ontology.semantic import store


def _db_ready() -> bool:
    try:
        return store.ping()
    except Exception:  # noqa: BLE001
        return False


db_required = pytest.mark.skipif(not _db_ready(), reason="需要可用的 PostgreSQL")


@pytest.fixture()
def schema():
    return SD.to_schema_dict(SD.default_semantic_schema())


@pytest.fixture()
def client():
    return TestClient(app, client=("127.0.0.1", 12345), raise_server_exceptions=False)


# ================================================================ 纯逻辑层
class TestSchemaAsCode:
    def test_default_schema_has_inheritance_and_characteristics(self, schema):
        assert "Manager" in schema["entity_types"]
        assert "Employee" in schema["entity_types"]
        assert "managed_by" in schema["relation_types"]
        assert schema["relation_types"]["collaborates_with"]["characteristics"]["symmetric"] is True
        assert schema["relation_types"]["reports_to"]["characteristics"]["transitive"] is True

    def test_definition_json_schema_exportable(self):
        js = SD.to_json_schema()
        assert js["type"] == "object"
        assert "$defs" in js and "EntityTypeDef" in js["$defs"]

    def test_instance_json_schema_required_and_enum(self, schema):
        emp = SD.instance_json_schema(schema, "Employee")
        assert emp["required"] == ["full_name"]
        assert emp["properties"]["employee_no"]["pattern"] == r"^E[0-9]{5}$"
        risk = SD.instance_json_schema(schema, "Risk")
        assert risk["required"] == ["risk_level"]
        assert risk["properties"]["risk_level"]["enum"] == ["low", "medium", "high"]

    def test_schema_dict_is_plain_stdlib(self, schema):
        # 纯 dict 投影，无 Pydantic 对象残留（供离线引擎消费）
        assert isinstance(schema["entity_types"]["Person"], dict)
        assert isinstance(schema["business_rules"], list)


class TestInheritanceReasoning:
    def test_manager_is_subtype_of_person(self, schema):
        assert C.is_subtype_of(schema, "Manager", "Person") is True
        assert C.is_subtype_of(schema, "Manager", "Employee") is True
        assert C.is_subtype_of(schema, "Project", "Person") is False

    def test_parent_query_hits_child_instances(self, schema):
        rows = [{"type_name": "Manager"}, {"type_name": "Project"},
                {"type_name": "Employee"}]
        out = R.instances_matching_type(schema, rows, "Person")
        assert {r["type_name"] for r in out} == {"Manager", "Employee"}

    def test_ancestors_chain_and_cycle_safe(self, schema):
        assert C.ancestors(schema, "Manager") == ["Manager", "Employee", "Person"]
        # 人为造环也不死循环
        cyc = dict(schema)
        cyc["entity_types"] = dict(schema["entity_types"])
        cyc["entity_types"]["Person"] = dict(cyc["entity_types"]["Person"], parent="Manager")
        chain = C.ancestors(cyc, "Manager")
        assert chain[0] == "Manager" and len(chain) == len(set(chain))


class TestSymmetricInverseTransitive:
    def test_symmetric_completion(self, schema):
        edges = [{"id": "e1", "source_id": "a", "relation_type": "collaborates_with",
                  "target_id": "b", "source_type": "Person", "target_type": "Person"}]
        out = R.symmetric_completion(schema, edges)
        assert len(out) == 1 and out[0]["source_id"] == "b"
        assert out[0]["derived"] is True and out[0]["derived_by"] == "symmetric"

    def test_inverse_completion(self, schema):
        edges = [{"id": "e2", "source_id": "p", "relation_type": "is_project_manager_of",
                  "target_id": "proj", "source_type": "Person", "target_type": "Project"}]
        out = R.inverse_completion(schema, edges)
        assert len(out) == 1 and out[0]["relation_type"] == "managed_by"
        assert out[0]["source_id"] == "proj" and out[0]["target_id"] == "p"

    def test_transitive_closure_depth_limited_and_cycle_safe(self, schema):
        edges = [
            {"id": "1", "source_id": "a", "relation_type": "reports_to", "target_id": "b"},
            {"id": "2", "source_id": "b", "relation_type": "reports_to", "target_id": "c"},
            {"id": "3", "source_id": "c", "relation_type": "reports_to", "target_id": "a"},  # 环
        ]
        out = R.transitive_closure(schema, edges, "reports_to", max_depth=3)
        pairs = {(x["source_id"], x["target_id"]) for x in out}
        assert ("a", "c") in pairs
        assert all(x["depth"] <= 3 for x in out)
        assert all(x["derived"] is True for x in out)
        # 已断言的边不重复产出
        assert ("a", "b") not in pairs

    def test_transitive_non_transitive_relation_returns_empty(self, schema):
        edges = [{"id": "1", "source_id": "a", "relation_type": "authored", "target_id": "d"}]
        assert R.transitive_closure(schema, edges, "authored") == []

    def test_expand_marks_all_as_candidates(self, schema):
        edges = [
            {"id": "e1", "source_id": "a", "relation_type": "collaborates_with",
             "target_id": "b", "source_type": "Person", "target_type": "Person"},
            {"id": "e2", "source_id": "a", "relation_type": "reports_to", "target_id": "c"},
            {"id": "e3", "source_id": "c", "relation_type": "reports_to", "target_id": "d"},
        ]
        exp = R.expand(schema, edges)
        # 对称补 1 条（b→a）；互逆 0 条；传递闭包 1 条（a→d，a→c/c→d 已断言不重复）
        assert len(exp["symmetric"]) == 1
        assert len(exp["inverse"]) == 0
        assert len(exp["transitive"]) == 1
        assert ("a", "d") in {(x["source_id"], x["target_id"]) for x in exp["transitive"]}
        assert exp["derived_total"] == len(exp["symmetric"]) + len(exp["inverse"]) + len(exp["transitive"])
        assert all(x["derived"] for x in exp["symmetric"] + exp["inverse"] + exp["transitive"])
        assert "未回写" in exp["note"]


class TestConstraintEnforcement:
    def test_relation_type_mismatch_rejected_with_clear_error(self, schema):
        rep = C.validate_relation(schema, {
            "source_id": "a", "relation_type": "reports_to", "target_id": "b",
            "source_type": "Project", "target_type": "Person"})
        assert rep["blocking"] is True
        assert rep["violations"][0]["constraint"] == "source_type"
        assert "Project" in rep["violations"][0]["message"]

    def test_relation_subtype_satisfies_parent(self, schema):
        rep = C.validate_relation(schema, {
            "source_id": "a", "relation_type": "reports_to", "target_id": "b",
            "source_type": "Manager", "target_type": "Employee"})
        assert rep["conforms"] is True

    def test_unregistered_relation_type(self, schema):
        rep = C.validate_relation(schema, {"source_id": "a", "relation_type": "nope",
                                           "target_id": "b"})
        assert rep["violations"][0]["constraint"] == "registered_type"

    def test_self_loop_rejected(self, schema):
        rep = C.validate_relation(schema, {"source_id": "a", "relation_type": "reports_to",
                                           "target_id": "a", "source_type": "Person",
                                           "target_type": "Person"})
        assert any(v["constraint"] == "self_loop" for v in rep["violations"])

    def test_required_attribute_missing(self, schema):
        rep = C.validate_instance(schema, {"type_name": "Employee", "properties": {}})
        assert any(v["constraint"] == "minCount" for v in rep["violations"])

    def test_enum_and_pattern_violations(self, schema):
        rep = C.validate_instance(schema, {"type_name": "Risk",
                                           "properties": {"risk_level": "extreme"}})
        assert any(v["constraint"] == "in" for v in rep["violations"])
        rep2 = C.validate_instance(schema, {"type_name": "Employee",
                                            "properties": {"employee_no": "bad"}})
        assert any(v["constraint"] == "pattern" for v in rep2["violations"])

    def test_assert_valid_raises_constraint_violation(self, schema):
        rep = C.validate_instance(schema, {"type_name": "Employee", "properties": {}})
        with pytest.raises(C.ConstraintViolation) as ei:
            C.assert_valid(rep, "实例")
        assert ei.value.report["blocking"] is True
        assert "必填" in str(ei.value)

    def test_cardinality_one_to_one(self, schema):
        base = {"source_id": "a", "relation_type": "works_at", "target_id": "org1",
                "source_type": "Person", "target_type": "OrganizationUnit"}
        existing = [{"source_id": "a", "relation_type": "works_at", "target_id": "org0",
                     "status": "active"}]
        rep = C.validate_relation(schema, base, existing_edges=existing)
        assert any(v["constraint"] == "cardinality" for v in rep["violations"])


class TestBusinessRules:
    def test_derivation_rule_emits_candidate_not_fact(self, schema):
        hit = RU.evaluate_for_write(schema, "write_relation",
                                    {"relation_type": "reports_to",
                                     "source_type": "Person", "target_type": "Person"})
        assert hit["matched"] >= 1
        assert any(a["code"] == "R-DERIVATION-01" for a in hit["applied_rules"])
        assert all(c["facts_written"] is False for c in hit["derived_candidates"])

    def test_authorization_rule_denies_restricted(self, schema):
        hit = RU.apply_rules(schema["business_rules"],
                             {"visibility": "restricted"}, schema)
        assert any(v["rule_type"] == "authorization" and v["severity"] == "violation"
                   for v in hit["violations"])

    def test_empty_condition_is_fail_closed(self, schema):
        assert RU.match_condition({}, {"anything": 1}) is False
        hit = RU.apply_rules([{"name": "empty", "condition": {}, "action": {"rule_type": "derivation"}}],
                             {"operation": "x"}, schema)
        assert hit["matched"] == 0

    def test_condition_dsl_all_any_in(self, schema):
        assert RU.match_condition({"all": [{"field": "a", "op": "eq", "value": 1},
                                           {"field": "b", "op": "in", "value": [2, 3]}]},
                                  {"a": 1, "b": 3}) is True
        assert RU.match_condition({"any": [{"field": "a", "op": "eq", "value": 9},
                                           {"field": "b", "op": "gt", "value": 2}]},
                                  {"a": 1, "b": 3}) is True

    def test_rule_payload_validation(self):
        assert RU.validate_rule_payload({"name": "r", "rule_type": "integrity",
                                         "condition": {"field": "x", "op": "eq", "value": 1},
                                         "action": {"assert": "y"}})["valid"] is True
        assert RU.validate_rule_payload({"name": "", "rule_type": "bad"})["valid"] is False


class TestServerContextAndFingerprint:
    def test_body_spoof_fields_detected(self):
        built = EX.build_server_context(
            {"tenant_id": "default", "actor": "owner"}, None,
            body={"tenant_id": "EVIL", "actor": "hacker", "ontology_version": "v9"})
        assert set(built["stripped_body_keys"]) >= {"tenant_id", "actor", "ontology_version"}
        assert built["context"]["tenant_id"] == "default"       # 未被污染
        assert built["context"]["actor"] == "owner"
        assert built["body_trusted"] is False

    def test_context_schema_version_is_server_side(self):
        built = EX.build_server_context({"tenant_id": "t1"}, None,
                                        body={"schema_version": "fake"})
        assert built["context"]["source"] == "global_middleware"
        assert "fake" not in built["context"]["schema_version"]

    def test_fingerprint_stable_for_same_core(self):
        core = {"state": "answered", "answer": {"count": 2},
                "evidence_refs": ["a"], "applied_rules": [], "derived_candidates": [],
                "query": "x"}
        assert EX._fingerprint(core) == EX._fingerprint(dict(core))


# ================================================================ 实库层
@db_required
class TestHealthAndSchemaAPI:
    def test_health_is_durable_postgres(self, client):
        r = client.get("/api/v1/ontology/semantic/health")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["durable"] is True and d["store"] == "postgres" and d["db_ok"] is True
        assert d["counts"]["semantic_schemas"] >= 1

    def test_schema_endpoint_exposes_inheritance(self, client):
        r = client.get("/api/v1/ontology/semantic/schema")
        assert r.status_code == 200
        d = r.json()
        assert "Manager" in d["entity_types"] and "managed_by" in d["relation_types"]
        assert "collaborates_with" in d["characteristics"]


@db_required
class TestConstraintRejectionAPI:
    def test_bad_relation_returns_400_with_violations(self, client):
        r = client.post("/api/v1/ontology/semantic/relations", json={
            "source_id": "00000000-0000-0000-0000-000000000001",
            "relation_type": "reports_to",
            "target_id": "00000000-0000-0000-0000-000000000002",
            "source_type": "Project", "target_type": "Person"})
        assert r.status_code == 400, r.text
        d = r.json()
        assert d["error"] == "constraint_violation"
        assert d["violations"] and d["violations"][0]["constraint"] == "source_type"

    def test_bad_instance_returns_400(self, client):
        r = client.post("/api/v1/ontology/semantic/instances", json={
            "type_name": "Employee", "canonical_name": "某某", "properties": {}})
        assert r.status_code == 400
        assert r.json()["error"] == "constraint_violation"

    def test_valid_instance_accepted_and_bound_to_version(self, client):
        r = client.post("/api/v1/ontology/semantic/instances", json={
            "type_name": "Employee", "canonical_name": "测试人员-用例自清理",
            "properties": {"full_name": "测试人员-用例自清理"}})
        assert r.status_code == 200, r.text
        assert r.json()["created"] is True and r.json()["ontology_version"]
        # 实库用例：测完即清，不向真实实例表残留数据
        from app.db import session as _dbs
        _dbs.execute("DELETE FROM ontology.entity_instances WHERE id = :i",
                     {"i": r.json()["id"]})


@db_required
class TestVersionFreeze:
    def test_freeze_is_immutable_and_resolves(self, client):
        tid = "task-test-immutable"
        r1 = client.post("/api/v1/ontology/semantic/snapshots/freeze", json={"task_id": tid})
        assert r1.status_code == 200, r1.text
        v1 = r1.json()["ontology_version"]
        r2 = client.post("/api/v1/ontology/semantic/snapshots/freeze", json={"task_id": tid})
        assert r2.json()["already_frozen"] is True
        assert r2.json()["ontology_version"] == v1
        resolved = client.get(f"/api/v1/ontology/semantic/snapshots/{tid}").json()
        assert resolved["bound"] is True and resolved["ontology_version"] == v1
        assert resolved["schema"]["entity_types"]  # 完整 schema 快照可还原


@db_required
class TestPermission:
    def test_non_owner_write_is_forbidden(self, client):
        r = client.post("/api/v1/ontology/semantic/instances",
                        json={"type_name": "Employee", "canonical_name": "x",
                              "properties": {"full_name": "x"}},
                        headers={"X-Permission-Context": '{"role":"member","scope":[]}'})
        assert r.status_code == 403


@db_required
class TestViolationsAPI:
    def test_violations_endpoint_shape(self, client):
        d = client.get("/api/v1/ontology/semantic/violations").json()
        assert d["constraint_mode"] == "application_enforced"
        assert "checked" in d and "violations" in d and "violation_count" in d

