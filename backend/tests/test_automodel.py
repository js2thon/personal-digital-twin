"""backend/tests/test_automodel.py —— 自动建模 / 指标 / 问答 / 前置修复（纯函数部分，无 DB）。

运行：cd backend && python -m pytest tests/test_automodel.py -q
"""
import pandas as pd
import pytest

from app.modules.ontology.automodel import connectors, metrics as mx, pipeline, qa
from app.modules.ontology.automodel.materializer import render


def _ts():
    ts = connectors.TableSet("hr", "file")

    def add(n, df):
        ts.tables[n] = connectors.Table(n, df, row_count=len(df))
    add("departments", pd.DataFrame({"dept_id": [10, 20], "dept_name": ["研发部", "销售部"],
                                     "parent_dept_id": [None, 10]}))
    add("employees", pd.DataFrame({
        "emp_no": [1, 2, 3, 4, 5, 6], "full_name": list("甲乙丙丁戊己"),
        "dept_id": [10, 20, 10, 20, 10, 20], "manager_emp_no": [None, 1, 1, 2, 2, 1],
        "level": ["P5", "P6", "P7", "P5", "P6", "P7"],
        "hire_date": ["2021-01-05", "2022-03-01", "2023-06-01", "2023-07-15", "2024-02-01", "2025-01-01"],
        "salary": [10000.0, 20000.0, 30000.0, 15000.0, 25000.0, 35000.0]}))
    add("projects", pd.DataFrame({"project_id": [1, 2], "project_name": ["数据中台", "CRM"],
                                  "owner_emp_no": [1, 2], "budget": [100, 60],
                                  "start_date": ["2025-01-01", "2025-03-01"]}))
    add("project_members", pd.DataFrame({"emp_no": [1, 2, 3, 4, 5], "project_id": [1, 1, 1, 2, 2]}))
    return ts


@pytest.fixture(scope="module")
def ctx():
    job = pipeline.run(_ts())
    schema = job["draft"]["schema"]
    ms = job["metrics"]
    F, L = mx.frames(job["materialized"], schema)
    return job, schema, ms, F, L


# ---------------------------------------------------------------- 建模
def test_relations_inferred(ctx):
    job, schema, *_ = ctx
    rels = schema["relation_types"]
    assert rels["works_at"]["target"] == "OrganizationUnit"
    assert rels["reports_to"]["source"] == rels["reports_to"]["target"] == "Person"
    assert rels["part_of"]["source"] == "OrganizationUnit"
    assert rels["owned_by"]["target"] == "Person"
    assert any(r["cardinality"] == "many_to_many" for r in rels.values())
    assert not [i for i in job["issues"] if i["level"] == "error"]


def test_materialize_no_orphans(ctx):
    job, *_ = ctx
    st = job["materialized"]["stats"]
    assert st["dangling"] == 0 and st["isolated_entities"] == 0
    rf = [f for f in job["materialized"]["facts"] if f["predicate"] == "reports_to"]
    assert rf and all(f["object_value"]["external_id"].startswith("hr:employees:") for f in rf)


def test_render_is_safe():
    assert render("x:{a}", {"a": 1.0}) == "x:1"
    assert render("x:{a.__class__}", {"a": 1}) == "x:{a.__class__}"
    assert render("x:{a}", {"a": None}) is None


def test_review_rename_and_reject(ctx):
    job = pipeline.run(_ts())
    pipeline.apply_review(job, [
        {"kind": "class", "ref": "Project", "action": "rename", "new_name": "Program", "label": "项目群"},
        {"kind": "relation", "ref": "project_member", "action": "accept"}])
    pipeline.rebuild(job, _ts())
    s = job["draft"]["schema"]
    assert "Program" in s["entity_types"] and "Project" not in s["entity_types"]
    assert s["relation_types"]["owned_by"]["source"] == "Program"
    assert not job["draft"]["review"]
    assert job["status"] == "ready_to_publish"


# ---------------------------------------------------------------- 指标
def test_metrics_all_evaluable(ctx):
    _, schema, ms, F, L = ctx
    assert not [i for i in ms["issues"] if i["level"] == "error"]
    for mid, m in ms["metrics"].items():
        mx.evaluate(mid, ms["metrics"], F, L, schema)
        if m["kind"] != "derived":
            for d in m["dims_allowed"]:
                mx.evaluate(mid, ms["metrics"], F, L, schema, d)


def test_metric_values(ctx):
    _, schema, ms, F, L = ctx
    M = ms["metrics"]
    assert mx.evaluate("person_count", M, F, L, schema) == 6
    assert mx.evaluate("salary_avg", M, F, L, schema) == 22500.0
    assert mx.evaluate("person_per_organization_unit", M, F, L, schema) == 3.0
    assert mx.evaluate("span_of_reports_to", M, F, L, schema) == 2.5      # 5 名有上级者 / 2 名上级
    assert mx.evaluate("budget_per_person", M, F, L, schema) == round(160 / 6, 3)
    by_dept = mx.evaluate("person_count", M, F, L, schema, "works_at__dept_name").to_dict()
    assert by_dept == {"研发部": 3, "销售部": 3}


def test_sql_compiles(ctx):
    job, schema, ms, *_ = ctx
    sql = mx.to_sql("person_count", ms["metrics"], schema, job["contract"], "works_at__dept_name")
    assert 'LEFT JOIN "departments" t1 ON t1."dept_id" = t0."dept_id"' in sql
    with pytest.raises(ValueError):
        mx._q('x"; drop table y; --')


# ---------------------------------------------------------------- 问答
def _num(r):
    res = r["result"]
    if "value" in res:
        return res["value"]
    if "count" in res:                                   # 本体图：按属性过滤计数
        return res["count"]
    return res["items"][0]["count"]                      # 本体图：沿关系聚合邻居


@pytest.mark.parametrize("q,expect", [
    ("一共有多少员工？", 6), ("销售部有多少人", 3), ("研发部的平均薪资", 21666.67),
    ("P7有多少人", 2), ("2023年后入职的人有多少", 4), ("平均管理幅度", 2.5),
    ("每个部门平均有多少人", 3.0), ("最高薪资", 35000.0),
])
def test_qa_scalar(ctx, q, expect):
    _, schema, ms, F, L = ctx
    r = qa.ask(q, schema, ms, F, L)
    assert r["ok"], r
    assert abs(_num(r) - expect) < 0.01, (q, r["answer"], r["plan"])


def test_qa_group_and_rank(ctx):
    _, schema, ms, F, L = ctx
    r = qa.ask("按职级统计平均薪资", schema, ms, F, L)
    assert dict(zip(r["result"]["index"], r["result"]["values"])) == {"P5": 12500.0, "P6": 22500.0, "P7": 32500.0}
    r = qa.ask("哪个职级平均薪资最高", schema, ms, F, L)
    assert r["result"]["index"] == ["P7"]
    r = qa.ask("每年入职人数", schema, ms, F, L)
    assert r["chart"]["type"] == "line"


def test_qa_list_and_unknown(ctx):
    _, schema, ms, F, L = ctx
    r = qa.ask("研发部有哪些人员", schema, ms, F, L)
    assert r["result"]["items"][0]["count"] == 3 and set(r["result"]["items"][0]["members"]) == {"甲", "丙", "戊"}
    r = qa.ask("天气怎么样", schema, ms, F, L)
    assert not r["ok"] and r["suggestions"]


def test_plan_validation_rejects_sql(ctx):
    _, schema, ms, *_ = ctx
    errs = qa.validate_plan({"op": "metric", "metric": "person_count", "sql": "select 1"}, schema, ms)
    assert any("sql" in e for e in errs)
    errs = qa.validate_plan({"op": "metric", "metric": "person_count", "dim": "nope"}, schema, ms)
    assert errs


class _FakeLLM:
    def __init__(self, plan): self.plan = plan
    def chat_json(self, *a, **k): return self.plan


def test_llm_plan_validated_then_fallback(ctx):
    _, schema, ms, F, L = ctx
    bad = _FakeLLM({"op": "metric", "metric": "no_such_metric"})
    r = qa.ask("平均薪资是多少", schema, ms, F, L, llm=bad)     # LLM 计划不合法 → 回落规则
    assert r["ok"] and r["engine"] == "rule"
    good = _FakeLLM({"op": "metric", "metric": "salary_avg", "dim": "level"})
    r = qa.ask("随便问", schema, ms, F, L, llm=good)
    assert r["engine"] == "llm" and r["result"]["kind"] == "series"


# ---------------------------------------------------------------- 发布格式 & 类型统一
def test_engine_definitions_valid(ctx):
    from app.modules.ontology.automodel import publish
    from app.modules.ontology.runtime import metric_engine as ME
    _, schema, ms, *_ = ctx
    defs = publish.to_engine_definitions(ms, schema, "v-test")
    assert ME.validate([dict(d) for d in defs])["valid"]


def test_merge_schema_conflict():
    from app.modules.ontology.automodel import publish
    cur = {"entity_types": {"Person": {}}, "relation_types": {
        "has_role": {"source": "Person", "target": "Role"}}, "attributes": {}}
    draft = {"entity_types": {"Person": {}}, "relation_types": {
        "has_role": {"source": "Person", "target": "OrganizationUnit"}}, "attributes": {}}
    assert publish.merge_schema(cur, draft, "v9")["conflicts"]


def test_type_registry_derive_inheritance():
    from app.modules.ontology import type_registry as tr
    schema = {"entity_types": {"Person": {"parent": ""}, "Employee": {"parent": "Person"},
                               "OrganizationUnit": {"parent": ""},
                               "Organization": {"parent": "OrganizationUnit"}},
              "relation_types": {"works_at": {"source": "Person", "target": "OrganizationUnit",
                                              "cardinality": "many_to_one"}}}
    d = tr.derive_relation_types(schema)["works_at"]
    assert set(d["source"]) == {"Person", "Employee"}
    assert set(d["target"]) == {"OrganizationUnit", "Organization"}
    assert d["cardinality"] == "one"


def test_type_registry_patch_keeps_builtin():
    from app.modules.ontology import graph, type_registry as tr
    cur = {"entity_types": {}, "relation_types": {"has_role": {"source": "Person",
                                                               "target": "OrganizationUnit"}}}
    out = tr.registry_patch(cur, graph.RELATION_TYPES)
    assert set(graph.RELATION_TYPES) <= set(out["relation_types"])
    assert out["relation_types"]["has_role"]["target"] == "Role"


# ---------------------------------------------------------------- 本体图问答（graphqa）
def _wide():
    ts = connectors.TableSet("up", "file")
    df = pd.DataFrame({"emplid": ["1", "2", "3", "4"],
                       "emp_name": ["林晓峰 (Lin Xiaofeng)", "周雨桐 (Zhou Yutong)", "韩子墨 (Han Zimo)", "秦远航 (Qin Yuanhang)"],
                       "post_cn_name": ["数据管理主管", "数据管理业务经理", "业务数据架构管理负责人", "数据管理主管"],
                       "dept_cn_name": ["业务数据架构管理", "职能数据架构管理", "数据合规组", "数据合规组"],
                       "grade": ["E07", "E06", "E08", "E10"], "sex_cn_name": ["男", "女", "男", "男"],
                       "age": [27, 28, 31, 44]})
    ts.tables["数据管理部人员"] = connectors.Table("数据管理部人员", df, row_count=4)
    job = pipeline.run(ts)
    s = job["draft"]["schema"]
    F, L = mx.frames(job["materialized"], s)
    return s, job["metrics"], F, L


@pytest.mark.parametrize("q,expect", [
    ("林晓峰的岗位是什么", "数据管理主管"), ("周雨桐在哪个部门", "职能数据架构管理"),
    ("秦远航的职级", "E10"), ("数据合规组有多少人", "共 2 个"), ("男女比例", "男 3"),
])
def test_graphqa_wide_table(q, expect):
    s, ms, F, L = _wide()
    r = qa.ask(q, s, ms, F, L)
    assert r["ok"] and r["engine"].startswith("ontology_graph"), r
    assert expect in r["answer"], (q, r["answer"])


def test_graphqa_wide_metric_with_filter():
    s, ms, F, L = _wide()
    r = qa.ask("数据合规组的平均年龄", s, ms, F, L)
    assert abs(r["result"]["value"] - 37.5) < 0.01


@pytest.mark.parametrize("q,expect", [
    ("乙的上级是谁", "汇报给→甲"), ("研发部有多少人", "共有 3 个"), ("数据中台的负责人是谁", "负责人→甲"),
])
def test_graphqa_relations(ctx, q, expect):
    _, schema, ms, F, L = ctx
    r = qa.ask(q, schema, ms, F, L)
    assert r["ok"] and expect in r["answer"], (q, r["answer"])


class _EchoLLM:
    def chat(self, system, user, **k):
        assert "依据" in user and "本体结构" in user        # 回答只能基于本体依据
        return "依据本体：" + user[:20]
    def chat_json(self, *a, **k):
        return {}


def test_grounded_answer_uses_ontology_evidence(ctx):
    _, schema, ms, F, L = ctx
    r = qa.ask("甲在哪个部门", schema, ms, F, L, llm=_EchoLLM())
    assert r["engine"] == "ontology_graph+llm" and r["answer"].startswith("依据本体")
    assert r["evidence"]["op"] == "lookup"


def test_graphqa_hierarchical_dept_filter_uses_all_levels():
    """「数据管理部」既是 1 人的末级部门，又是 27 人的 5 级部门：应按全部层级列取并集。"""
    rows = [{"emplid": str(i), "emp_name": f"人{i}", "dept_cn_name_lv5": "数据管理部",
             "dept_cn_name": "数据管理部" if i == 0 else ("数据合规组" if i % 3 == 0 else "数据治理组"),
             "sex_cn_name": "男" if i < 17 else "女", "age": 25 + i} for i in range(27)]
    ts = connectors.TableSet("up", "file")
    ts.tables["数据管理部人员"] = connectors.Table("数据管理部人员", pd.DataFrame(rows), row_count=27)
    job = pipeline.run(ts)
    s = job["draft"]["schema"]
    F, L = mx.frames(job["materialized"], s)
    r = qa.ask("数据管理部男女占比多少？", s, job["metrics"], F, L)
    assert r["result"]["total"] == 27 and "男 17" in r["answer"] and "女 10" in r["answer"], r["answer"]
    r = qa.ask("数据合规组有多少人", s, job["metrics"], F, L)
    assert r["result"]["count"] == 8


def test_evaluate_generates_and_scores(ctx):
    from app.modules.ontology.automodel import evaluate as ev
    _, schema, ms, F, L = ctx
    qs = ev.generate(schema, F)
    kinds = {q["kind"] for q in qs}
    assert {"count_all", "attr_lookup", "relation", "negative"} <= kinds
    res = ev.run(qs, lambda q: qa.ask(q, schema, ms, F, L))
    assert res["accuracy"] >= 0.95, [i for i in res["items"] if not i["pass"]]
    # 判分器本身要能识别错误答案
    bad = ev.score({"gold": {"type": "number", "value": 10}}, {"ok": True, "result": {"value": 3}, "answer": "3"})
    assert not bad["pass"]
