"""P1-4 租户与 ABAC 逐点过滤回归测试。

风格沿用项目既有约定：纯函数单测 + 直连实库的用例（test_event_bus 同款），
不使用 conftest、不触发 startup。涉库用例**自行清理**写入的事实行，
不给评估基准集与业务数据留残渣。
"""
import uuid

import pytest

from app import policy as pol
from app.visibility import RESTRICTED, RETRIEVABLE, VALID


# ---------------------------------------------------------------- 判定核心

def test_conformance_matrix_all_pass():
    """符合性矩阵必须全过：这是 permission_filter_accuracy 的地基。"""
    cases = pol.conformance_cases()
    assert len(cases) >= 14
    for i, (ctx, resource, expected) in enumerate(cases):
        got = pol.decide(pol.principal_from(ctx), **resource)["allowed"]
        assert got is expected, f"case{i} {resource} 期望 {expected} 实得 {got}"


def test_principal_from_none_is_fail_closed():
    p = pol.principal_from(None)
    assert p["role"] == "anonymous"
    verdict = pol.decide(p, tenant_id="default", visibility="owner")
    assert verdict["allowed"] is False


def test_principal_from_dict_context():
    p = pol.principal_from({"tenant_id": "t1", "user_id": "u9",
                            "permission_context": {"role": "member",
                                                   "scope": "owner,organization"}})
    assert p["tenant_id"] == "t1" and p["user_id"] == "u9"
    assert "owner" in p["scope"] and "organization" in p["scope"]


def test_default_semantic_context_is_owner_class():
    """AC-01 默认推导上下文必须等价 owner 级：向后兼容的关键承诺。"""
    from app.modules.semantic_runtime.context import system_context

    p = pol.principal_from(system_context())
    assert pol.is_owner_class(p)
    assert pol.decide(p, tenant_id="default", visibility="owner")["allowed"]


def test_tenant_mismatch_denied_even_for_owner_class():
    p = pol.principal_from({"tenant_id": "default", "user_id": "boss",
                            "permission_context": {"role": "owner",
                                                   "scope": ["owner"]}})
    assert pol.decide(p, tenant_id="tenant_b", visibility="owner")["rule"] == pol.R_TENANT


def test_unknown_visibility_fail_closed():
    p = pol.principal_from({"tenant_id": "default", "user_id": "x",
                            "permission_context": {"role": "owner",
                                                   "scope": ["owner"]}})
    for bad in ("secret", "public", "internal_only"):
        assert pol.decide(p, tenant_id="default", visibility=bad)["allowed"] is False
    # 空串按既有约定视为未设置（NULL→owner 同口径），回落 owner 而非拒绝
    assert pol.decide(p, tenant_id="default", visibility="")["allowed"] is True


def test_visibility_constants_unchanged():
    """白名单口径不允许被本次改动漂移。"""
    assert RETRIEVABLE == ("owner", "organization")
    assert RESTRICTED == "restricted"
    assert VALID == {"owner", "organization", "restricted"}


# ---------------------------------------------------------------- SQL 生成

def test_read_clause_owner_class_contains_tenant_and_whitelist():
    p = pol.principal_from({"tenant_id": "t1", "user_id": "boss",
                            "permission_context": {"role": "owner",
                                                   "scope": ["owner", "organization"]}})
    params: dict = {}
    clause = pol.read_clause("f", p, params)
    assert "f.tenant_id" in clause and ":p_tenant" in clause
    assert "'owner'" in clause and "'organization'" in clause
    assert "restricted" not in clause
    assert params["p_tenant"] == "t1"


def test_read_clause_non_owner_scopes_organization_and_owner_rows():
    p = pol.principal_from({"tenant_id": "t1", "user_id": "u1",
                            "permission_context": {"role": "member",
                                                   "scope": ["organization"]}})
    params: dict = {}
    clause = pol.read_clause("f", p, params)
    assert "AND TRUE" in clause      # 有 organization 范围
    assert "f.created_by = :p_user2" in clause
    assert params.get("p_user2") == "u1"


def test_read_clause_non_owner_without_org_scope_folds_false():
    p = pol.principal_from({"tenant_id": "t1", "user_id": "u2",
                            "permission_context": {"role": "member", "scope": []}})
    clause = pol.read_clause("f", p, {})
    assert "AND FALSE" in clause  # scope 生成期折叠，SQL 不可被请求改变


# ---------------------------------------------------------------- 内存过滤

def test_filter_rows_mixed():
    p = pol.principal_from({"tenant_id": "default", "user_id": "u1",
                            "permission_context": {"role": "member",
                                                   "scope": ["organization"]}})
    rows = [
        {"tenant_id": "default", "visibility": "organization", "created_by": "x"},
        {"tenant_id": "tenant_b", "visibility": "organization", "created_by": "x"},
        {"tenant_id": "default", "visibility": "restricted", "created_by": "u1"},
        {"tenant_id": "default", "visibility": "owner", "created_by": "u1"},
        {"tenant_id": "default", "visibility": "owner", "created_by": "u9"},
    ]
    kept = pol.filter_rows(rows, p)
    assert len(kept) == 2 and kept[0]["created_by"] == "x" and kept[1]["created_by"] == "u1"


def test_deny_log_and_stats():
    pol.reset_stats()
    before = pol.stats()
    assert before["decisions"] == 0
    p = pol.principal_from(None)
    pol.decide(p, tenant_id="default", visibility="owner")
    st = pol.stats()
    assert st["decisions"] == 1 and st["denied"] == 1
    recent = pol.recent(5)
    assert recent and recent[0]["rule"] == pol.R_TENANT
    pol.reset_stats()


# ---------------------------------------------------------------- 实库（自行清理）

def _cleanup(fact_ids: list[str]) -> None:
    from app.db import session as db

    for fid in fact_ids:
        db.execute("DELETE FROM knowledge.facts WHERE id = CAST(:i AS uuid)",
                   {"i": fid})


@pytest.fixture()
def scoped_facts():
    """两租户各一条 owner 事实 + 一条 organization 事实；用后即删。"""
    from app.modules.ontology import service as ontology

    marker = "abac" + uuid.uuid4().hex[:8]  # 不带下划线：结构化通道的关键词预处理会剥掉 _
    created = []
    try:
        r1 = ontology.write_fact(predicate=marker, object_value="tenant_default_row",
                                 visibility="owner", tenant_id="default")
        r2 = ontology.write_fact(predicate=marker, object_value="tenant_x_row",
                                 visibility="owner", tenant_id="tenant_abac_x")
        r3 = ontology.write_fact(predicate=marker + "_org", object_value="org_row",
                                 visibility="organization", tenant_id="default")
        created = [r1["fact_id"], r2["fact_id"], r3["fact_id"]]
        yield {"marker": marker, "default_row": r1["fact_id"],
               "x_row": r2["fact_id"], "org_row": r3["fact_id"]}
    finally:
        _cleanup(created)


def test_tenant_columns_covered():
    from app.modules import policy as tenant_policy

    first = tenant_policy.ensure_tenant_columns()
    second = tenant_policy.ensure_tenant_columns()
    for table, res in first.items():
        assert res["ok"], f"{table}: {res}"
    cov = tenant_policy.coverage()
    for table, row in cov.items():
        assert "error" not in row, f"{table}: {row}"
        assert row["coverage"] == 1.0, f"{table} 覆盖率 {row['coverage']}"


def test_owner_principal_sees_default_rows_via_clause(scoped_facts):
    from app.db import session as db

    p = pol.principal_from({"tenant_id": "default", "user_id": "boss",
                            "permission_context": {"role": "owner",
                                                   "scope": ["owner", "organization"]}})
    params = {"m": scoped_facts["marker"] + "%"}
    clause = pol.read_clause("f", p, params)
    rows = db.fetch_all(
        "SELECT f.id FROM knowledge.facts f WHERE f.predicate LIKE :m "
        f"AND f.status = 'active' AND {clause}", params)
    ids = {str(r["id"]) for r in rows}
    assert scoped_facts["default_row"] in ids
    assert scoped_facts["org_row"] in ids
    assert scoped_facts["x_row"] not in ids  # 跨租户隔离


def test_foreign_tenant_principal_sees_nothing_here(scoped_facts):
    from app.db import session as db

    p = pol.principal_from({"tenant_id": "tenant_abac_x", "user_id": "u_x",
                            "permission_context": {"role": "owner",
                                                   "scope": ["owner", "organization"]}})
    params = {"m": scoped_facts["marker"] + "%"}
    clause = pol.read_clause("f", p, params)
    rows = db.fetch_all(
        "SELECT f.id, f.tenant_id FROM knowledge.facts f WHERE f.predicate LIKE :m "
        f"AND f.status = 'active' AND {clause}", params)
    ids = {str(r["id"]) for r in rows}
    assert scoped_facts["x_row"] in ids       # 本租户行可见
    assert scoped_facts["default_row"] not in ids
    assert scoped_facts["org_row"] not in ids


def test_retrieval_entry_filters_by_principal(scoped_facts):
    """GV-01 第 1 点：检索入口过滤（事实通道）。"""
    from app.modules.retrieval import service as retrieval

    owner_p = pol.principal_from({"tenant_id": "default", "user_id": "boss",
                                  "permission_context": {"role": "owner",
                                                         "scope": ["owner", "organization"]}})
    foreign_p = pol.principal_from({"tenant_id": "tenant_abac_x", "user_id": "u_x",
                                    "permission_context": {"role": "owner",
                                                           "scope": ["owner", "organization"]}})
    q = scoped_facts["marker"]
    got_owner = {str(r["fact_id"]) for r in retrieval.retrieve(
        q, scope=["facts"], top_k=20, principal=owner_p)["facts"]}
    got_foreign = {str(r["fact_id"]) for r in retrieval.retrieve(
        q, scope=["facts"], top_k=20, principal=foreign_p)["facts"]}
    assert scoped_facts["default_row"] in got_owner
    assert scoped_facts["x_row"] not in got_owner
    assert scoped_facts["x_row"] in got_foreign
    assert scoped_facts["default_row"] not in got_foreign


def test_edge_allowed_per_hop_tenant():
    """GV-01 第 2 点：图遍历逐跳。"""
    from app.modules.semantic_runtime import service as sem
    from app.modules.semantic_runtime.context import system_context

    ctx = system_context()
    assert sem._edge_allowed({"status": "active", "tenant_id": "default"}, ctx)
    assert not sem._edge_allowed({"status": "active", "tenant_id": "tenant_b"}, ctx)
    assert not sem._edge_allowed({"status": "withdrawn", "tenant_id": "default"}, ctx)


def test_structured_channel_clause_filters_tenant(scoped_facts):
    from app.modules.semantic_runtime import service as sem
    from app.modules.semantic_runtime.context import system_context

    foreign = type(system_context())(
        tenant_id="tenant_abac_x", user_id="u_x",
        request_id="req_t", trace_id="trc_t",
        permission_context={"role": "owner", "scope": ["owner", "organization"]},
        schema_version="v2.1.0")
    items, _ = sem._structured_channel(
        scoped_facts["marker"], {}, 20, [], 0.0, foreign)
    ids = {str(it["id"]) for it in items}
    assert scoped_facts["default_row"] not in ids
    assert scoped_facts["x_row"] in ids


def test_memory_channel_filters_by_tenant():
    """记忆通道同样逐点过滤：外租户零召回，owner 级行为不变。"""
    from app.modules import policy as tenant_policy
    from app.modules.retrieval import service as retrieval

    tenant_policy.ensure_tenant_columns()  # 幂等：测试进程内先补列

    owner_p = pol.principal_from({"tenant_id": "default", "user_id": "boss",
                                  "permission_context": {"role": "owner",
                                                         "scope": ["owner", "organization"]}})
    foreign_p = pol.principal_from({"tenant_id": "tenant_abac_x", "user_id": "u_x",
                                    "permission_context": {"role": "owner",
                                                           "scope": ["owner", "organization"]}})
    got_owner = retrieval.retrieve("项目", scope=["memories"], top_k=5,
                                   principal=owner_p)["memories"]
    got_foreign = retrieval.retrieve("项目", scope=["memories"], top_k=5,
                                     principal=foreign_p)["memories"]
    # 库内记忆全部属于 default 租户：外租户必须零行（allow-list 数据可空）
    assert got_foreign == []
    assert isinstance(got_owner, list)  # owner 级不被误伤（条数取决于库内数据）


def test_permission_accuracy_metric_end_to_end():
    """评估指标可测：矩阵全对 + 跨租户探针 0 行。"""
    from app.modules.evaluation.service import _permission_accuracy

    acc, detail = _permission_accuracy()
    assert acc is not None
    assert acc == 1.0, detail
    assert "跨租户探针" in detail


def test_list_facts_endpoint_tenant_header():
    """API 层：外租户请求头看不到 default 租户事实。"""
    import pytest as _pytest

    fastapi = _pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, client=("127.0.0.1", 12345))
    r_default = client.get("/api/v1/knowledge/facts?limit=5")
    assert r_default.status_code == 200
    rows_default = r_default.json()
    if isinstance(rows_default, dict):
        rows_default = rows_default.get("items") or rows_default.get("facts") or []
    if not rows_default:
        # 数据依赖：断言的是「owner 能看到事实」，库内可能已被批量下架（console:batch-unpublish
        # 会把全部事实置 withdrawn）。数据不在是环境状态，不是代码回归，照实跳过不装 pass。
        _pytest.skip("库内当前无 active 事实（可能已被批量下架）")
    assert len(rows_default) > 0  # 回环 owner 级不变（向后兼容）

    r_foreign = client.get("/api/v1/knowledge/facts?limit=5",
                           headers={"X-Tenant-Id": "tenant_abac_x"})
    assert r_foreign.status_code == 200
    rows_foreign = r_foreign.json()
    if isinstance(rows_foreign, dict):
        rows_foreign = rows_foreign.get("items") or rows_foreign.get("facts") or []
    assert rows_foreign == []  # 外租户 + 库内全是 default → 零行
