"""知识图谱内核回归：关系类型目录 / 子图 SQL 约束 / 组装 / 质量统计。

全部是纯函数用例，不连数据库 —— 图查询的 SQL 只验「构造是否安全且语义正确」，
真实执行结果由 8101 的端到端实测覆盖。
"""
from app.modules.ontology import graph as og


def _node(i, name, etype="Person", status="active"):
    return {"id": i, "canonical_name": name, "entity_type": etype, "status": status}


def _edge(s, t, rtype, status="active", eid=None):
    return {"id": eid or f"e-{s}-{t}", "source_entity_id": s, "target_entity_id": t,
            "relation_type": rtype, "status": status, "evidence_claim_id": None}


# ---------------------------------------------------------------- 类型目录

def test_classify_registered_and_unknown():
    ok = og.classify("reports_to")
    assert ok["registered"] and ok["label"] == "汇报给" and ok["family"] == "org"
    bad = og.classify("will_know_after_signing")
    assert bad["registered"] is False and bad["family"] == "other"
    assert bad["label"] == "will_know_after_signing"


def test_relation_catalog_sorts_by_usage_and_appends_unregistered():
    rows = og.relation_catalog({"reports_to": 3, "will_know_after_signing": 1})
    assert rows[0]["type"] == "reports_to" and rows[0]["used"] == 3
    unknown = [r for r in rows if not r["registered"]]
    assert [r["type"] for r in unknown] == ["will_know_after_signing"]
    assert unknown[0]["family_label"] == "未登记"


def test_type_violation_flags_wrong_target_type():
    meta = og.classify("has_role")
    assert og.type_violation(meta, "Person", "Role") is None
    assert og.type_violation(meta, "Person", "Project") == "target_type_mismatch(Project)"
    assert og.type_violation(og.classify("unknown_rel"), "Person", "Person") == \
        "unregistered_relation_type"


def test_wildcard_types_accept_anything():
    meta = og.classify("part_of")   # source/target 都是 "*"
    assert og.type_violation(meta, "System", "Component") is None


# ---------------------------------------------------------------- 参数钳制

def test_clamp_depth_and_limit():
    assert og.clamp_depth(0) == 1
    assert og.clamp_depth(9) == og.MAX_DEPTH
    assert og.clamp_depth("x") == 2
    assert og.clamp_limit(0) == 1
    assert og.clamp_limit(10 ** 6) == og.MAX_NODES


def test_parse_types_rejects_unsafe_tokens():
    assert og.parse_types("reports_to, works_on ,,reports_to") == ["reports_to", "works_on"]
    # 引号/分号/空格注入一律丢弃
    assert og.parse_types("a'; DROP TABLE x; --") == []
    assert og.parse_types(None) == []


# ---------------------------------------------------------------- SQL 构造

def test_subgraph_sql_has_cycle_guard_and_bound_params(monkeypatch):
    q = og.subgraph_queries("11111111-1111-1111-1111-111111111111", 5, ["reports_to"])
    nodes_sql, params = q["nodes"]
    assert "WITH RECURSIVE walk" in nodes_sql
    assert "NOT (nb.next_id = ANY(w.path))" in nodes_sql     # 环保护必须在
    assert params["depth"] == og.MAX_DEPTH                    # 5 跳被钳到 3
    assert params["root"] == "11111111-1111-1111-1111-111111111111"
    assert '"reports_to"' in params["types_json"]
    # 类型不是拼进 SQL 的，而是走参数
    assert "reports_to" not in nodes_sql


def test_subgraph_without_center_lists_entities():
    q = og.subgraph_queries(None, 2, [])
    nodes_sql, params = q["nodes"]
    assert "WITH RECURSIVE" not in nodes_sql
    assert "status IN ('active', 'candidate')" in nodes_sql
    assert params["types_json"] == "[]"


def test_paths_query_clamps_depth():
    sql, params = og.paths_query("a" * 8, "b" * 8, 99)
    assert "WITH RECURSIVE search" in sql
    assert "NOT (nb.next_id = ANY(s.path))" in sql
    assert params["depth"] == og.MAX_PATH_DEPTH


# ---------------------------------------------------------------- 组装

def test_assemble_drops_dangling_edges_and_counts_degree():
    nodes = [_node("a", "本人"), _node("b", "项目", "Project"), _node("c", "孤岛", "Concept")]
    edges = [_edge("a", "b", "works_on"), _edge("a", "ghost", "works_on")]
    g = og.assemble_graph(nodes, edges)
    assert {n["id"] for n in g["nodes"]} == {"a", "b", "c"}
    assert len(g["edges"]) == 1                       # 悬空边被丢掉
    deg = {n["id"]: n["degree"] for n in g["nodes"]}
    assert deg["a"] == 1 and deg["b"] == 1 and deg["c"] == 0
    assert g["stats"]["isolated"] == 1
    assert g["stats"]["edges"] == 1


def test_assemble_marks_invalid_edges():
    nodes = [_node("p", "某某", "Person"), _node("x", "项目", "Project")]
    g = og.assemble_graph(nodes, [_edge("p", "x", "has_role")])
    edge = g["edges"][0]
    assert edge["valid"] is False
    assert edge["problem"] == "target_type_mismatch(Project)"
    assert g["stats"]["violations"] == 1


def test_hide_isolated_removes_degree_zero_nodes():
    nodes = [_node("a", "A"), _node("b", "B"), _node("c", "C")]
    g = og.assemble_graph(nodes, [_edge("a", "b", "knows")], isolated_hidden=True)
    assert [n["id"] for n in g["nodes"]] == ["a", "b"]
    assert g["stats"]["isolated"] == 1               # 统计仍如实报孤立点


def test_density_and_truncation():
    nodes = [_node(i, f"n{i}") for i in range(5)]
    g = og.assemble_graph(nodes, [], limit=5)
    assert g["truncated"] is True                    # 命中 limit
    assert g["stats"]["density"] == 0.0


# ---------------------------------------------------------------- 质量统计

def test_components_counts_isolated_as_own_component():
    ids = ["a", "b", "c", "d"]
    assert og.components(ids, [("a", "b")]) == 3     # {a,b} + c + d
    assert og.components(ids, [("a", "b"), ("c", "d")]) == 2
    assert og.components(ids, []) == 4


def test_shape_stats_reports_isolated_and_unregistered():
    ents = [_node("a", "本人"), _node("b", "项目", "Project"), _node("c", "公司", "Organization")]
    rels = [_edge("a", "b", "works_on"), _edge("a", "c", "will_know_after_signing")]
    s = og.shape_stats(ents, rels)
    assert s["entities"] == 3 and s["relations"] == 2
    assert s["isolated"] == 0
    assert s["components"] == 1
    assert s["unregistered_types"] == ["will_know_after_signing"]
    assert s["relation_types_used"] == 2
    assert s["top_degree"][0]["id"] == "a" and s["top_degree"][0]["degree"] == 2


def test_shape_stats_ignores_non_active_relations():
    ents = [_node("a", "A"), _node("b", "B")]
    rels = [_edge("a", "b", "knows", status="deprecated")]
    s = og.shape_stats(ents, rels)
    assert s["relations"] == 0
    assert s["isolated"] == 2


def test_shape_stats_flags_type_violations():
    ents = [_node("p", "某某", "Person"), _node("x", "项目", "Project")]
    s = og.shape_stats(ents, [_edge("p", "x", "has_role")])
    assert s["type_violations"] == 1
    assert s["violation_samples"][0]["problem"] == "target_type_mismatch(Project)"


# ---------------------------------------------------------------- 路径可读化

def test_describe_paths_builds_arrow_text():
    rows = [{"path": ["a", "b", "c"], "rels": ["reports_to", "is_owner_of"], "depth": 2}]
    names = {"a": "本人", "b": "某某丙", "c": "项目"}
    types = {"a": "Person", "b": "Person", "c": "Project"}
    out = og.describe_paths(rows, names, types)
    assert out[0]["text"] == "本人 → -[汇报给]-> 某某丙 → -[负责]-> 项目"
    assert len(out[0]["hops"]) == 2
    assert out[0]["hops"][0]["registered"] is True
    assert out[0]["nodes"][2]["type"] == "Project"


def test_describe_paths_handles_unregistered_relation():
    rows = [{"path": ["a", "b"], "rels": ["mystery"], "depth": 1}]
    out = og.describe_paths(rows, {"a": "A", "b": "B"}, {})
    assert out[0]["hops"][0]["registered"] is False
    assert out[0]["hops"][0]["label"] == "mystery"
