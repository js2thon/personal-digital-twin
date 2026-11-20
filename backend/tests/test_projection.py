"""事实→关系投影的规则测试（纯函数，不连库）。

重点覆盖三件真实出过问题的事：
1. 结构化事实（{"value": ...}）必须能建边——某某的 reports_to/works_at 曾经完全丢图；
2. 泛称实体（「项目」「团队」）不能被包含匹配吃掉；
3. 类型不合法（Person has_role Project）不许生成边。
"""
from app.modules.ontology import projection as proj


def ent(eid, name, etype, aliases=None, external_ids=None):
    e = {"id": eid, "canonical_name": name, "entity_type": etype,
         "aliases": aliases or []}
    if external_ids:
        e["external_ids"] = external_ids
    return e


def fact(fid, subj, pred, value):
    return {"id": fid, "claim_id": "c-" + fid, "subject_id": subj,
            "predicate": pred, "object_value": {"value": value}}


ENTITIES = [
    ent("p1", "某某", "Person", ["Mou Mou"]),
    ent("p2", "某某乙", "Person", ["Mou Mou B"]),
    ent("org1", "公司", "Organization", ["公司"]),
    ent("team", "团队", "Organization"),
    ent("proj", "项目", "Project"),
    ent("proj2", "数据安全项目", "Project"),
    ent("role1", "项目经理", "Role"),
]


def test_normalize_strips_parenthesis():
    assert proj.normalize_value("某某乙 (Mou Mou B)") == "某某乙"
    assert proj.normalize_value("  某某（Zhao） ") == "某某"


def test_pick_object_text_variants():
    assert proj.pick_object_text({"value": "公司"}) == "公司"
    assert proj.pick_object_text({"name": "某某乙"}) == "某某乙"
    assert proj.pick_object_text("某某") == "某某"
    assert proj.pick_object_text({"list": ["a"]}) is None
    assert proj.pick_object_text(None) is None


def test_structured_fact_projects_edge():
    """结构化事实（value 键）此前完全进不了图，这是本次要修的核心缺陷。"""
    facts = [fact("f1", "p1", "reports_to", "某某乙 (Mou Mou B)"),
             fact("f2", "p1", "works_at", "公司")]
    out = proj.project_facts(facts, ENTITIES)
    triples = {(e["source_entity_id"], e["relation_type"], e["target_entity_id"])
               for e in out["edges"]}
    assert ("p1", "reports_to", "p2") in triples
    assert ("p1", "works_at", "org1") in triples


def test_attribute_predicates_never_become_edges():
    facts = [fact("f3", "p1", "level", "E09"),
             fact("f4", "p1", "department-2", "CEO")]
    out = proj.project_facts(facts, ENTITIES)
    assert out["edges"] == []
    assert out["stats"]["attribute"] == 2


def test_generic_entity_does_not_swallow_long_value():
    """「项目」不能把「数据安全项目」吃掉——覆盖率阈值挡住了它。"""
    facts = [fact("f6", "p1", "is_project_manager_of", "数据安全项目")]
    out = proj.project_facts(facts, ENTITIES)
    targets = {e["target_entity_id"] for e in out["edges"]}
    assert "proj2" in targets and "proj" not in targets


def test_type_mismatch_rejected():
    """Person -has_role-> Project 是历史脏边，投影必须拒绝生成。"""
    facts = [fact("f7", "p1", "has_role", "项目")]
    out = proj.project_facts(facts, ENTITIES)
    assert out["edges"] == []
    assert out["stats"]["type_mismatch"] == 1


def test_unregistered_predicate_skipped():
    facts = [fact("f8", "p1", "will_know_after_signing", "某某乙")]
    out = proj.project_facts(facts, ENTITIES)
    assert out["edges"] == []
    assert out["stats"]["unregistered"] == 1


def test_self_loop_and_duplicate_skipped():
    facts = [fact("f9", "p1", "collaborates_with", "某某"),
             fact("f10", "p1", "collaborates_with", "某某乙"),
             fact("f11", "p1", "collaborates_with", "某某乙")]
    out = proj.project_facts(facts, ENTITIES)
    assert len(out["edges"]) == 1
    assert out["stats"]["self_loop"] == 1
    assert out["stats"]["duplicate"] == 1


def test_unknown_value_unresolved():
    facts = [fact("f12", "p1", "works_at", "某家不存在的公司")]
    out = proj.project_facts(facts, ENTITIES)
    assert out["edges"] == []
    assert out["stats"]["unresolved"] == 1


def test_alias_match():
    facts = [fact("f13", "p1", "reports_to", "Mou Mou B")]
    out = proj.project_facts(facts, ENTITIES)
    assert [e["target_entity_id"] for e in out["edges"]] == ["p2"]


def test_diff_add_remove():
    desired = [{"source_entity_id": "a", "relation_type": "knows", "target_entity_id": "b"},
               {"source_entity_id": "a", "relation_type": "knows", "target_entity_id": "c"}]
    existing = [{"id": "r1", "source_entity_id": "a", "relation_type": "knows",
                 "target_entity_id": "b"},
                {"id": "r2", "source_entity_id": "a", "relation_type": "knows",
                 "target_entity_id": "x"}]
    plan = proj.diff(existing, desired)
    assert plan["stats"]["add"] == 1
    assert plan["stats"]["remove"] == 1
    assert plan["stats"]["keep"] == 1
    assert plan["to_remove"][0]["id"] == "r2"


def test_pick_object_entity_id_variants():
    assert proj.pick_object_entity_id({"entity_id": "e-1"}) == "e-1"
    assert proj.pick_object_entity_id({"target_id": "e-2"}) == "e-2"
    assert proj.pick_object_entity_id({"entity_id": "  "}) is None
    assert proj.pick_object_entity_id({"value": "某某"}) is None
    assert proj.pick_object_entity_id("某某") is None


def test_project_prefers_entity_id_over_changed_name():
    """人工边回写的事实用 entity_id 定位：名字改了也照样对得上。"""
    facts = [{"id": "f-manual", "claim_id": None, "subject_id": "p1",
              "predicate": "reports_to",
              "object_value": {"entity_id": "p2", "name": "某某乙（旧名）"}}]
    out = proj.project_facts(facts, ENTITIES)
    assert out["stats"]["projected"] == 1
    assert out["stats"]["by_entity_id"] == 1
    edge = out["edges"][0]
    assert edge["target_entity_id"] == "p2"
    assert edge["relation_type"] == "reports_to"


def test_project_entity_id_must_exist_locally():
    """id 指向的实体不存在时，退回名字匹配而不是丢事实（宁可多试一步）。"""
    facts = [{"id": "f-x", "claim_id": None, "subject_id": "p1",
              "predicate": "reports_to",
              "object_value": {"entity_id": "deleted-id", "name": "某某乙"}}]
    out = proj.project_facts(facts, ENTITIES)
    assert out["stats"]["projected"] == 1
    assert out["edges"][0]["target_entity_id"] == "p2"


def test_entity_ref_resolved_by_external_id():
    """结构化 entity_ref 只给工号：name 栏也是工号，必须靠 external_ids 认人。"""
    ents = [ent("p1", "某某", "Person"),
            ent("p2", "某某乙", "Person", external_ids=["file:397078"])]
    facts = [{"id": "f-ext", "claim_id": None, "subject_id": "p1",
              "predicate": "reports_to",
              "object_value": {"name": "397078", "external_id": "file:397078"}}]
    out = proj.project_facts(facts, ents)
    assert out["stats"]["by_external_id"] == 1
    assert out["stats"]["unresolved"] == 0
    assert len(out["edges"]) == 1
    assert out["edges"][0]["target_entity_id"] == "p2"


def test_ambiguous_external_id_is_not_guessed():
    """同一个工号挂在两条档案上时不猜，宁可漏一条边。"""
    ents = [ent("p1", "某某", "Person"),
            ent("p2", "某某乙", "Person", external_ids=["file:397078"]),
            ent("p3", "某某丙", "Person", external_ids=["file:397078"])]
    facts = [{"id": "f-amb", "claim_id": None, "subject_id": "p1",
              "predicate": "reports_to",
              "object_value": {"name": "397078", "external_id": "file:397078"}}]
    out = proj.project_facts(facts, ents)
    assert out["edges"] == []
    assert out["stats"]["unresolved"] == 1


def test_missing_external_id_falls_back_to_name():
    """外部标识查不到时不直接判死，仍给名字匹配一次机会。"""
    ents = [ent("p1", "某某", "Person"), ent("p2", "某某乙", "Person")]
    facts = [{"id": "f-fb", "claim_id": None, "subject_id": "p1",
              "predicate": "reports_to",
              "object_value": {"name": "某某乙", "external_id": "file:404"}}]
    out = proj.project_facts(facts, ents)
    assert out["edges"][0]["target_entity_id"] == "p2"
    assert out["stats"]["external_missing"] == 1
