"""本体规则引擎测试：既要证明「该报的报」，也要证明「不该报的不报」。"""
from app.modules.ontology import quality

ALLOWED = {"Person", "Project", "Organization", "Role", "System"}


def ent(eid, name, etype, status="active"):
    return {"id": eid, "canonical_name": name, "entity_type": etype, "status": status}


def rel(rid, s, t, rt, ev="c1", status="active"):
    return {"id": rid, "source_entity_id": s, "target_entity_id": t,
            "relation_type": rt, "evidence_claim_id": ev, "status": status}


def by_id(result):
    return {r["id"]: r for r in result["rules"]}


def test_clean_data_passes_every_rule():
    entities = [ent("p1", "张三", "Person"), ent("p2", "李四", "Person"),
                ent("o1", "某公司", "Organization")]
    relations = [rel("r1", "p1", "p2", "reports_to"), rel("r2", "p1", "o1", "works_at")]
    res = quality.run_rules(entities, relations, [], ALLOWED)
    rules = by_id(res)
    for rid in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10"):
        assert rules[rid]["count"] == 0, rid
    assert res["summary"]["verdict"] == "pass"


def test_unregistered_type_detected():
    res = quality.run_rules([ent("p1", "张三", "Person"), ent("p2", "李四", "Person")],
                            [rel("r1", "p1", "p2", "made_up")], [], ALLOWED)
    assert by_id(res)["R1"]["count"] == 1


def test_type_constraint_violation():
    res = quality.run_rules(
        [ent("p1", "张三", "Person"), ent("j1", "项目A", "Project")],
        [rel("r1", "p1", "j1", "has_role")], [], ALLOWED)
    assert by_id(res)["R2"]["count"] == 1
    assert by_id(res)["R2"]["severity"] == "error"


def test_single_valued_conflict():
    res = quality.run_rules(
        [ent("p1", "张三", "Person"), ent("p2", "李四", "Person"),
         ent("p3", "王五", "Person")],
        [rel("r1", "p1", "p2", "reports_to"), rel("r2", "p1", "p3", "reports_to")],
        [], ALLOWED)
    assert by_id(res)["R3"]["count"] == 1


def test_self_loop_duplicate_and_missing_evidence():
    res = quality.run_rules(
        [ent("p1", "张三", "Person"), ent("o1", "某公司", "Organization")],
        [rel("r1", "p1", "p1", "manages"),
         rel("r2", "p1", "o1", "works_at", ev=None),
         rel("r3", "p1", "o1", "works_at")], [], ALLOWED)
    rules = by_id(res)
    assert rules["R4"]["count"] == 1
    assert rules["R5"]["count"] == 1
    assert rules["R7"]["count"] == 1


def test_dangling_endpoint_detected():
    res = quality.run_rules(
        [ent("p1", "张三", "Person"), ent("p9", "已合并", "Person", status="merged")],
        [rel("r1", "p1", "p9", "manages")], [], ALLOWED)
    assert by_id(res)["R6"]["count"] == 1


def test_bad_entity_profile():
    res = quality.run_rules(
        [ent("p3", "", "Person"), ent("x1", "幽灵", "Gadget"),
         ent("p1", "张三", "Person")], [], [], ALLOWED)
    assert by_id(res)["R8"]["count"] == 2


def test_attribute_value_naming_entity_flagged():
    facts = [{"id": "f1", "subject_id": "p1", "predicate": "manager_id",
              "object_value": {"value": "李四"}}]
    res = quality.run_rules([ent("p1", "张三", "Person"), ent("p2", "李四", "Person")],
                            [], facts, ALLOWED)
    assert by_id(res)["R9"]["count"] == 1


def test_isolated_entity_is_info_only():
    res = quality.run_rules(
        [ent("p1", "张三", "Person"), ent("o1", "某公司", "Organization"),
         ent("p2", "孤岛", "Person")],
        [rel("r1", "p1", "o1", "works_at")], [], ALLOWED)
    assert by_id(res)["R10"]["count"] == 1
    assert by_id(res)["R10"]["severity"] == "info"
    assert res["summary"]["errors"] == 0


def test_selftest_passes():
    """规则引擎自测必须通过——这条挂了说明尺子本身坏了。"""
    st = quality.selftest()
    assert st["passed"] is True, st
    assert set(st["expected_rules"]) <= set(st["hit_rules"])
