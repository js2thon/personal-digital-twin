"""结构化接入回归：映射契约 / 确定性断言 / 安全闸门 / 链路校验口径。

绝大多数用例是纯函数，不依赖数据库；涉及 DB 的用 monkeypatch 打桩，
保证「符合项目目标设定」的关键约束可以离线验证。
"""
import copy

from app.modules.structured import assertions as az
from app.modules.structured import contracts as ct
from app.modules.structured import service as svc
from app.visibility import is_retrievable, retrievable_clause

RAW = {
    "entity": {"entity_type": "Person", "pk_column": "employee_id",
               "name_column": "employee_name", "external_id_prefix": "hr:"},
    "fields": [
        {"column": "dept_name", "predicate": "belongs_to_department",
         "object_kind": "literal", "visibility": "organization",
         "valid_from_column": "effective_from", "required": True},
        {"column": "manager_id", "predicate": "reports_to",
         "object_kind": "entity_ref", "target_entity_type": "Person",
         "target_name_column": "manager_name", "relation_type": "reports_to",
         "visibility": "organization"},
    ],
    "sensitivity": {"default": "organization", "restricted_columns": []},
    "allowed_values": {},
    "expect_columns": ["employee_id", "employee_name", "dept_name",
                       "manager_id", "manager_name", "effective_from"],
}


def mapping(**overrides):
    raw = copy.deepcopy(RAW)
    raw.update(overrides)
    errors, clean = ct.validate_mapping(raw)
    assert not errors, errors
    return clean


def row(pk="E1001", dept="数据管理部", manager="", **extra):
    base = {"employee_id": pk, "employee_name": "某某丙", "dept_name": dept,
            "manager_id": manager, "manager_name": "", "effective_from": "2025-07-01"}
    base.update(extra)
    return base


def build(rows, m=None, synced_at="2026-11-12T00:00:00+00:00"):
    return az.build_assertions(
        mapping=m or mapping(), rows=rows, contract_name="hr_employee_org",
        version=1, contract_id="00000000-0000-0000-0000-000000000011",
        contract_hash="h" * 64,
        source_ref={"system": "hr", "table": "dw_employee"},
        batch_id="sync-test", synced_at=synced_at,
    )


# ---------------------------------------------------------------- 契约校验

def test_validate_mapping_accepts_valid_contract():
    errors, clean = ct.validate_mapping(RAW)
    assert errors == []
    assert clean["entity"]["pk_column"] == "employee_id"
    assert clean["fields"][0]["visibility"] == "organization"
    assert clean["expect_columns"]


def test_validate_mapping_rejects_unknown_entity_type_and_visibility():
    bad = copy.deepcopy(RAW)
    bad["entity"]["entity_type"] = "EmployeeTable"   # 不允许新知识造新类型
    bad["fields"][0]["visibility"] = "public_everywhere"
    errors, _ = ct.validate_mapping(bad)
    assert any("六域" in e for e in errors)
    assert any("visibility" in e for e in errors)


def test_validate_mapping_rejects_empty_fields_and_duplicate_predicate():
    errors, _ = ct.validate_mapping({"entity": RAW["entity"], "fields": []})
    assert errors
    dup = copy.deepcopy(RAW)
    dup["fields"].append(dict(dup["fields"][0]))
    errors, _ = ct.validate_mapping(dup)
    assert any("重复" in e for e in errors)


def test_contract_hash_stable_regardless_of_key_order():
    a = {"x": 1, "y": {"p": 2, "q": 3}}
    b = {"y": {"q": 3, "p": 2}, "x": 1}
    assert ct.contract_hash(a, {"table": "t"}) == ct.contract_hash(b, {"table": "t"})
    assert ct.contract_hash(a, {"table": "t"}) != ct.contract_hash(a, {"table": "u"})


# ---------------------------------------------------------------- 确定性

def test_assertions_are_deterministic():
    rows = [row(), row(pk="E1002", manager="E1001")]
    a = build(rows)
    b = build(rows)
    assert a["assertions"] == b["assertions"]
    assert a["anomalies"] == b["anomalies"]


def test_row_hash_ignores_key_order_but_detects_value_change():
    assert az.row_hash({"a": 1, "b": 2}) == az.row_hash({"b": 2, "a": 1})
    assert az.row_hash({"a": 1}) != az.row_hash({"a": 2})


def test_idempotency_key_excludes_batch_and_sync_time():
    """重复同步值不变时必须落在同一条断言上，否则幂等形同虚设。"""
    a = build([row()], synced_at="2026-11-12T00:00:00+00:00")
    b = build([row()], synced_at="2026-11-13T09:00:00+00:00")
    assert [x["idempotency_key"] for x in a["assertions"]] == \
           [x["idempotency_key"] for x in b["assertions"]]
    assert a["assertions"][0]["source_ref"]["channel"] == "structured_sync"
    for key in ("system", "table", "pk", "batch_id", "row_hash",
                "mapping_version", "idempotency_key"):
        assert a["assertions"][0]["source_ref"].get(key), key


# ---------------------------------------------------------------- 异常前置判定

def test_schema_drift_rejects_whole_batch():
    out = build([{"employee_id": "E1", "employee_name": "张三", "extra": "x"}])
    assert out["schema_ok"] is False
    assert out["assertions"] == []
    assert out["anomalies"][0]["reason"] == "schema_drift"


def test_missing_required_and_enum_invalid():
    m = mapping(allowed_values={"dept_name": ["数据管理部", "产品部"]})
    out = build([row(dept=""), row(pk="E2", dept="非法部门")], m=m)
    reasons = {a["reason"] for a in out["anomalies"]}
    assert "missing_required" in reasons and "enum_invalid" in reasons
    assert out["stats"]["assertions"] == 0


def test_multi_manager_blocked_before_write():
    """reports_to 是单值维度：两个不同主管必须拦截，不能让 write_fact 静默取代。"""
    out = build([row(manager="E9001"), row(manager="E9002")])
    assert any(a["reason"] == "multi_manager" for a in out["anomalies"])
    predicates = [a["predicate"] for a in out["assertions"]]
    assert "reports_to" not in predicates
    assert predicates.count("belongs_to_department") == 2


def test_cycle_detected_in_reporting_chain():
    out = build([row(pk="E1", manager="E2"), row(pk="E2", manager="E1")])
    assert any(a["reason"] == "cycle_detected" for a in out["anomalies"])
    assert all(a["relation_type"] == "" for a in out["assertions"])


def test_self_reference_keeps_other_reporting_edges():
    """部门负责人把主管填成本人：只丢他这一条，同批其他人的汇报关系必须留下。

    真实事故：27 行 HR 数据里 4 个负责人的 manager 字段是自己的工号，旧逻辑把它
    当环，整批 reports_to 全被丢弃 —— 27 个人在图上一条汇报边都没有。
    """
    out = build([row(pk="E1001", manager="E1001"),
                 row(pk="E1002", manager="E1001"),
                 row(pk="E1003", manager="E1002")])
    reasons = {a["reason"] for a in out["anomalies"]}
    assert "self_reference" in reasons
    assert "cycle_detected" not in reasons
    rel = [a for a in out["assertions"] if a["relation_type"] == "reports_to"]
    assert sorted(a["pk"] for a in rel) == ["E1002", "E1003"]
    assert all(a["object_ref"]["pk_value"] != a["pk"] for a in rel)


def test_cross_batch_self_reference_is_not_a_cycle():
    rows = [row(pk="E1", manager="E1"), row(pk="E2", manager="E1")]
    hits = az.cross_batch_anomalies(mapping=mapping(), rows=rows)
    reasons = {a["reason"] for a in hits}
    assert "self_reference" in reasons
    assert "cycle_detected" not in reasons
    _blocked, drop_relations = az.blocked_by(hits)
    assert drop_relations is False


def test_restricted_columns_never_produce_assertions():
    m = mapping(sensitivity={"default": "organization",
                             "restricted_columns": ["dept_name"]})
    out = build([row()], m=m)
    assert out["stats"]["restricted_skipped"] == 1
    assert out["stats"]["assertions"] == 0
    assert any(a["reason"] == "restricted_skipped" for a in out["anomalies"])


def test_reason_meta_covers_owner_place_and_verification():
    for code in ct.REASON_META:
        meta = ct.reason_meta(code)
        assert meta["owner"] and meta["where"] and meta["verify"] and meta["steps"]


# ---------------------------------------------------------------- 安全闸门

def test_visibility_is_fail_closed():
    assert is_retrievable("owner") and is_retrievable("organization")
    assert is_retrievable(None)          # 默认 owner
    assert not is_retrievable("restricted")
    assert not is_retrievable("public_everywhere")
    assert "IN ('owner', 'organization')" in retrievable_clause("f")
    assert "f.visibility" in retrievable_clause("f")


def test_sync_requires_explicit_confirmation():
    """未经本人确认不得写库；preview 也不能被 confirm=false 悄悄变成写入。"""
    out = svc.sync({"id": "x"}, [], "owner", confirm=False)
    assert out["error"] == "confirmation_required"


def test_draft_contract_cannot_be_used(monkeypatch):
    monkeypatch.setattr(ct, "ensure_schema", lambda: None)
    monkeypatch.setattr(ct.db, "fetch_one", lambda *a, **k: {
        "id": "c1", "name": "hr", "version": 1, "status": "draft"})
    contract, err = ct.published_contract_or_error("c1")
    assert contract is None
    assert err["error"] == "contract_not_published"


def test_owner_correction_is_not_overwritten_by_sync():
    """C1：同步写入 tier=2，本人手工修正 tier=3 → 落 contested，绝不静默取代。"""
    from app.modules.ontology import service as ontology

    decision = ontology.decide_write(
        {"same_value": False, "trust_tier": 3, "valid_from": "2025-01-01"},
        {"trust_tier": 2, "valid_from": "2026-01-01", "object_value": '{"value":"x"}'},
    )
    assert decision["status"] == ontology.FACT_CONTESTED


def test_structured_write_uses_agent_tier():
    """契约批准的是规则，不是值；同步值按 agent(2) 写入。"""
    assert svc.ontology.trust_tier_of("agent") == 2
    assert svc.ontology.trust_tier_of("owner") == 3


# ---------------------------------------------------------------- 链路校验口径

def test_facts_to_memory_denominator_excludes_structured(monkeypatch):
    """G1：结构化事实无 claim_id，不能把记忆链路覆盖率拉红。"""
    from app.api.v1 import lineage

    m = {
        "docs_with_chunks": 10, "docs_with_runs": 10,
        "approved_claims": 10, "approved_with_fact": 10, "approved_corroborated": 0,
        "active_facts": 100, "memory_linkable_facts": 40,   # 60 条为结构化事实
        "facts_with_memory": 36,                            # 36/40=0.9 通过；36/100 会误报
        "claims_with_proposal": 10, "real_relations": 5,
        "structured_facts": 60, "structured_untraced": 0,
        "decisions": 2, "decisions_with_evidence": 2,
        "decisions_with_snapshot": 2, "orphan_decisions": 0,
    }
    calls = {"n": 0}

    def fake_fetch_one(sql, params=None):
        calls["n"] += 1
        return m if calls["n"] == 2 else {}

    monkeypatch.setattr(lineage.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(lineage.db, "fetch_all", lambda *a, **k: [])
    out = lineage.overview()
    assert out["link_checks"]["facts_to_memory"] is True
    assert out["link_checks"]["structured_facts_traced"] is True
    assert out["link_details"]["facts_to_memory_denominator"] == 40


def test_structured_facts_traced_turns_false_when_source_ref_missing(monkeypatch):
    from app.api.v1 import lineage

    m = {
        "docs_with_chunks": 10, "docs_with_runs": 10,
        "approved_claims": 10, "approved_with_fact": 10,
        "active_facts": 10, "memory_linkable_facts": 10, "facts_with_memory": 10,
        "claims_with_proposal": 10, "real_relations": 5,
        "structured_facts": 2, "structured_untraced": 1,
        "decisions": 1, "decisions_with_evidence": 1,
        "decisions_with_snapshot": 1, "orphan_decisions": 0,
    }
    calls = {"n": 0}

    def fake_fetch_one(sql, params=None):
        calls["n"] += 1
        return m if calls["n"] == 2 else {}

    monkeypatch.setattr(lineage.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(lineage.db, "fetch_all", lambda *a, **k: [])
    assert lineage.overview()["link_checks"]["structured_facts_traced"] is False


def test_review_verification_backfills_recovered_or_still_failing(monkeypatch):
    """处置后由下一次批次自动回填验证结论，闭环不悬空。"""
    pending = [
        {"id": "a1", "reason": "enum_invalid", "pk": "E1", "field": "dept_name"},
        {"id": "a2", "reason": "multi_manager", "pk": "E2", "field": "reports_to"},
    ]
    written = []
    monkeypatch.setattr(svc.db, "execute",
                        lambda sql, params=None: written.append(params))
    n = svc._verify_reviews(
        pending, [{"reason": "multi_manager", "pk": "E2", "field": "reports_to"}])
    assert n == 2
    results = {w["i"]: w["v"] for w in written}
    assert results["a1"] == "recovered"
    assert results["a2"] == "still_failing"


# ---------------------------------------------------------------- 自动分批

CONTRACT = {"id": "00000000-0000-0000-0000-000000000011", "name": "hr_employee_org",
            "version": 1, "contract_hash": "h" * 64, "mapping": mapping(),
            "source_ref": {"system": "hr", "table": "dw_employee"}}


class _FakeTx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_pipeline(monkeypatch, *, batch_rows=2):
    """把分批编排涉及的 DB / 审计整体打桩：只验证「怎么切、怎么聚合、何时停」。"""
    recorded, written = [], []
    monkeypatch.setattr(svc, "BATCH_ROWS", batch_rows)
    monkeypatch.setattr(svc.ct, "ensure_schema", lambda: None)
    monkeypatch.setattr(svc.db, "transaction", lambda *a, **k: _FakeTx())
    monkeypatch.setattr(svc, "_pending_review_ids", lambda cid: [])
    monkeypatch.setattr(svc, "_verify_reviews", lambda pending, anomalies: 0)
    monkeypatch.setattr(svc, "_record_batch",
                        lambda **kw: recorded.append(kw) or f"rid{len(recorded)}")
    monkeypatch.setattr(svc, "_ensure_entity", lambda *a, **k: ("eid", False))
    monkeypatch.setattr(svc, "_find_entity", lambda *a, **k: {"id": "eid2"})
    monkeypatch.setattr(svc, "_ensure_relation", lambda *a, **k: "inserted")
    monkeypatch.setattr(svc.ontology, "write_fact",
                        lambda **kw: written.append(kw) or {"action": "inserted"})
    from app.modules.audit import service as audit
    monkeypatch.setattr(audit, "log_access", lambda *a, **k: None)
    return recorded, written


def test_plan_batches_keeps_row_order_and_respects_size(monkeypatch):
    monkeypatch.setattr(svc, "BATCH_ROWS", 2)
    rows = [row(pk=f"E{i}") for i in range(5)]
    chunks = svc._plan_batches(rows)
    assert [len(c) for c in chunks] == [2, 2, 1]
    # 行序必须保持：调岗「取代链」依赖先后，不能按主键重排
    assert [c[0]["employee_id"] for c in chunks] == ["E0", "E2", "E4"]


def test_batch_name_is_readable():
    assert svc._batch_name("g", 1, 1) == "g"
    assert svc._batch_name("g", 2, 3) == "g#02/03"


def test_cross_batch_multi_manager_found_when_rows_land_in_different_batches():
    """单批内查不出、跨批才暴露的冲突，必须在拆分前用整文件预检拦住。"""
    rows = [row(pk="E1", dept="数据管理部"), row(pk="E1", dept="产品部")]
    per_batch = [az.build_assertions(
        mapping=mapping(), rows=[r], contract_name="hr_employee_org", version=1,
        contract_id="c", contract_hash="h" * 64, source_ref={}, batch_id="b")["anomalies"]
        for r in rows]
    assert per_batch == [[], []]          # 各自单行，批内无冲突

    hits = az.cross_batch_anomalies(mapping=mapping(), rows=rows)
    assert [a["reason"] for a in hits] == ["multi_manager"]
    blocked, drop = az.blocked_by(hits)
    assert ("E1", "belongs_to_department") in blocked and drop is False


def test_cross_batch_cycle_found_across_batches():
    rows = [row(pk="E1", manager="E2"), row(pk="E2", manager="E1")]
    hits = az.cross_batch_anomalies(mapping=mapping(), rows=rows)
    assert any(a["reason"] == "cycle_detected" for a in hits)
    _, drop = az.blocked_by(hits)
    assert drop is True


def test_file_guard_drops_conflicting_predicate_from_batch():
    prepared = build([row(pk="E1", dept="数据管理部")])
    assert prepared["assertions"]
    prepared = svc._apply_file_guard(prepared, {("E1", "belongs_to_department")}, False)
    assert prepared["assertions"] == []
    assert prepared["stats"]["assertions"] == 0
    assert prepared["stats"]["by_predicate"] == {}


def test_preview_splits_batches_but_writes_nothing(monkeypatch):
    recorded, written = _stub_pipeline(monkeypatch)
    out = svc.preview(CONTRACT, [row(pk=f"E{i}") for i in range(5)], "owner")
    assert written == []                       # 预览绝不写库
    assert out["batch_count"] == 3 and out["batch_rows"] == 2
    assert out["stats"]["rows"] == 5 and out["assertions_total"] == 5
    assert len(recorded) == 1                  # 预览只留一条批次留痕
    assert recorded[0]["counts"]["batches"] == 3
    assert recorded[0]["dry_run"] is True


def test_sync_commits_every_batch_and_sums_counts(monkeypatch):
    recorded, written = _stub_pipeline(monkeypatch)
    out = svc.sync(CONTRACT, [row(pk=f"E{i}") for i in range(5)], "owner", confirm=True)
    assert out["partial"] is False and out["batch_count"] == 3
    assert len(written) == 5                   # 每一行都写到了，没有被批次吞掉
    assert out["counts"]["inserted"] == 5
    assert out["stats"]["rows"] == 5
    assert [r.get("status", "completed") for r in recorded] == ["completed"] * 3


def test_sync_stops_after_failed_batch_and_reports_partial(monkeypatch):
    recorded, written = _stub_pipeline(monkeypatch)
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("db write failed")
        written.append(kw)
        return {"action": "inserted"}

    monkeypatch.setattr(svc.ontology, "write_fact", flaky)
    out = svc.sync(CONTRACT, [row(pk=f"E{i}") for i in range(5)], "owner", confirm=True)
    assert out["partial"] is True
    assert out["failed_batch"] == 2 and out["batches_committed"] == 1
    assert len(written) == 2                   # 失败批整体回滚，后续批次不执行
    assert recorded[-1]["status"] == "failed"
    assert recorded[-1]["error_info"]["batch"] == 2


def test_schema_drift_rejects_whole_file_before_any_write(monkeypatch):
    recorded, written = _stub_pipeline(monkeypatch)
    bad = [dict(row(pk=f"E{i}"), performance_score="A") for i in range(3)]
    out = svc.sync(CONTRACT, bad, "owner", confirm=True)
    assert out["rolled_back"] is True and out["schema_ok"] is False
    assert written == [] and len(recorded) == 1
    assert recorded[0]["status"] == "rolled_back"
    assert out["stats"]["rows"] == 3


# ------------------------------------------- 实体归一：同一个人不能裂成两个画像

def test_ensure_entity_reuses_same_name_entity_without_anchor(monkeypatch):
    """文档蒸馏建的实体没有 external_ids；结构化同步必须按名字认领，
    否则某某/某某丙会被两条入径各建一档（画像出现两个同名 Person）。"""
    import json as _json
    state = {"updates": [], "inserted": 0}

    def fake_fetch_one(sql, params=None):
        if "external_ids" in sql:
            return None                      # 锚点查不到
        return {"id": "e-doc", "canonical_name": "某某",
                "aliases": ["某某"], "properties": {}}

    monkeypatch.setattr(svc.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(svc.db, "execute",
                        lambda sql, params=None: state["updates"].append(params))
    monkeypatch.setattr(svc.db, "insert_returning_id",
                        lambda *a, **k: state.__setitem__("inserted", state["inserted"] + 1))

    eid, created = svc._ensure_entity("file:hr-move-001", "Person", "某某", ["某某"])
    assert (eid, created) == ("e-doc", False)
    assert state["inserted"] == 0            # 绝不新建第二个某某
    props = _json.loads(state["updates"][0]["p"])
    assert "file:hr-move-001" in props["external_ids"]


def test_ensure_entity_prefers_external_id_over_name(monkeypatch):
    """已有锚点直接命中，不再看名字。"""
    monkeypatch.setattr(svc.db, "fetch_one", lambda sql, params=None: (
        {"id": "e-hr", "canonical_name": "某某",
         "aliases": [], "properties": {"external_ids": ["file:hr-move-001"]}}
        if "external_ids" in sql else (_ for _ in ()).throw(AssertionError("不应走到名字查询"))))
    monkeypatch.setattr(svc.db, "execute", lambda *a, **k: None)
    eid, created = svc._ensure_entity("file:hr-move-001", "Person", "另一个人名", [])
    assert (eid, created) == ("e-hr", False)


def test_ensure_entity_creates_when_no_anchor_and_no_same_name(monkeypatch):
    monkeypatch.setattr(svc.db, "fetch_one", lambda sql, params=None: None)
    monkeypatch.setattr(svc.db, "insert_returning_id", lambda *a, **k: "e-new")
    eid, created = svc._ensure_entity("file:9001", "Person", "新人", ["新人"])
    assert (eid, created) == ("e-new", True)
