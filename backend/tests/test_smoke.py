"""冒烟测试：不依赖外部服务，验证核心纯逻辑。"""
from app.modules.document_parser.parser import chunk_text, normalize_text
from app.modules.llm.client import stage_idempotency_key
from app.modules.policy.engine import check_tool, evaluate_decision
from app.modules.twin.orchestrator import route_task


def test_chunking_produces_locators():
    text = "段落一。\n\n段落二，内容更长一些。" * 40
    chunks = chunk_text(text, max_chars=200, overlap=50)
    assert chunks
    for c in chunks:
        assert c["content"]
        assert c["locator"]["char_end"] > c["locator"]["char_start"]


def test_idempotency_key_stable():
    a = stage_idempotency_key("doc1", "hash1", "qwen")
    b = stage_idempotency_key("doc1", "hash1", "qwen")
    assert a == b and len(a) == 64
    assert stage_idempotency_key("doc1", "hash2", "qwen") != a


def test_tool_whitelist_default_deny():
    assert check_tool("send_email")["verdict"] == "denied"
    assert check_tool("query_personal_knowledge")["verdict"] == "allowed"
    assert check_tool("unknown_tool")["verdict"] == "denied"


def test_mvp_never_auto_executes():
    verdict = evaluate_decision("low", action=None)
    assert verdict["execution_authorized"] is False
    assert verdict["level"] == "D2"


def test_routing():
    assert route_task("帮我回复这封邮件") == "communication"
    assert route_task("这个项目应该批准吗？") == "decision"
    assert route_task("我之前做过什么项目？") == "knowledge_qa"


def test_dws_distill_status_greys_completed_items(monkeypatch):
    """已完成 → completed（置灰）；进行中 → active；失败/待复核 → 仍可蒸馏。"""
    from app.modules.dws_source import distill_status as ds

    rows = [
        {"title": "员工隐私协议授权签署指导手册.pdf", "metadata": {}, "status": "COMPLETED"},
        {"title": "员工隐私协议授权签署指导手册", "metadata": {}, "status": "PENDING"},
        {"title": "数据管理部周会", "metadata": {"source_kind": "minutes",
                                             "external_ref": "M1"}, "status": "PENDING"},
        {"title": "本地上传文件", "metadata": {"source_kind": "upload"}, "status": "COMPLETED"},
        {"title": "失败文档", "metadata": {}, "status": "NEEDS_REVIEW"},
    ]
    monkeypatch.setattr(ds.db, "fetch_all", lambda *a, **k: rows)
    out = ds.states_for("document", [
        {"node_id": "N1", "name": "员工隐私协议授权签署指导手册", "doc_url": ""},
        {"node_id": "M1", "name": "数据管理部周会", "doc_url": ""},
        {"node_id": "U1", "name": "本地上传文件", "doc_url": ""},
        {"node_id": "F1", "name": "失败文档", "doc_url": ""},
        {"node_id": "X1", "name": "未蒸馏", "doc_url": ""},
    ])
    assert out["states"] == ["completed", "none", "none", "none", "none"]
    assert out["counts"]["completed"] == 1
    minutes = ds.states_for("minutes", [
        {"node_id": "M1", "name": "数据管理部周会", "doc_url": ""}])
    assert minutes["states"] == ["active"]


def test_normalize():
    assert normalize_text("a\r\nb") == "a\nb"


def test_lexical_terms_cover_chinese_and_english():
    """中文短语 + 汉字 2-gram + 英文词，疑问词不得成为检索词项。"""
    from app.modules.retrieval import lexical

    terms = [t["term"] for t in lexical.extract_terms("AppKey 授权是什么？数据管理部周会")]
    assert "AppKey".lower() in [t.lower() for t in terms]
    assert any("数据管理" in t for t in terms)
    assert "什么" not in terms and "么" not in "".join(terms)
    expr, params = lexical.score_expr(lexical.extract_terms("周会"), "c.content", "dk")
    assert "ILIKE" in expr and params and all(v.startswith("%") for v in params.values())
    assert abs(lexical.cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9


def test_identity_gate_default_denies_remote():
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.auth import actor_from_request
    from app.config import settings

    def req(host, key=None):
        headers = [(b"x-twin-api-key", key.encode())] if key else []
        return Request({"type": "http", "client": (host, 1234), "headers": headers,
                        "method": "POST", "path": "/api/v1/x", "scheme": "http",
                        "server": ("127.0.0.1", 8101), "root_path": "",
                        "query_string": b""})

    assert actor_from_request(req("127.0.0.1")) == "owner"
    try:
        actor_from_request(req("10.1.2.3"))
        raise AssertionError("remote request should be denied")
    except HTTPException as e:
        assert e.status_code == 401
    original = settings.api_key
    try:
        settings.api_key = "unit-test-key"
        assert actor_from_request(req("10.1.2.3", "unit-test-key")) == "owner"
    finally:
        settings.api_key = original


def test_proxy_headers_are_not_trusted_by_default():
    """M7：默认关闭代理信任，伪造 XFF 也不能把远程请求伪装成本人。"""
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.auth import actor_from_request
    from app.config import settings

    def req(host, forwarded=None):
        headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
        return Request({"type": "http", "client": (host, 1234), "headers": headers,
                        "method": "GET", "path": "/api/v1/x", "scheme": "http",
                        "server": ("127.0.0.1", 8101), "root_path": "",
                        "query_string": b""})

    # 远程直连 + 伪造回环 XFF：默认不采信
    try:
        actor_from_request(req("10.1.2.3", "127.0.0.1"))
        raise AssertionError("spoofed XFF must not bypass the gate")
    except HTTPException as e:
        assert e.status_code == 401

    original = (settings.trust_proxy_headers, settings.trusted_proxy_hosts)
    try:
        settings.trust_proxy_headers = True
        settings.trusted_proxy_hosts = "127.0.0.1"
        # 可信代理身后的远程用户 → 仍须 API Key
        for spoofed in ("10.1.2.3", "127.0.0.1, 10.9.9.9"):
            try:
                actor_from_request(req("127.0.0.1", spoofed))
                raise AssertionError("remote user behind proxy must be denied")
            except HTTPException as e:
                assert e.status_code == 401
        # 代理链全部可信 → 回落到对端地址（本机），仍视为本人
        assert actor_from_request(req("127.0.0.1", "127.0.0.1")) == "owner"
    finally:
        settings.trust_proxy_headers, settings.trusted_proxy_hosts = original


def test_tool_whitelist_exposes_real_coverage():
    """L13：策略清单必须自报覆盖度，不能把「仅声明」伪装成已拦截。"""
    from app.modules.policy import engine as policy

    cov = policy.tool_coverage()
    assert cov["total"] == len(policy.TOOL_WHITELIST)
    assert cov["wired"] >= 1 and cov["declared_only"] >= 1
    assert cov["wired"] + cov["implicit"] + cov["declared_only"] == cov["total"]
    assert policy.check_tool("send_message")["enforcement"] == "wired"
    assert policy.check_tool("send_email")["verdict"] == "denied"


def test_minio_without_credentials_degrades_safely():
    """L14：未显式配置对象存储凭据时，绝不拿代码内默认口令去连接。"""
    from app.config import settings
    from app.modules.ingestion import storage

    original = (settings.minio_access_key, settings.minio_secret_key)
    try:
        settings.minio_access_key = ""
        settings.minio_secret_key = ""
        assert storage.get_minio() is None
    finally:
        settings.minio_access_key, settings.minio_secret_key = original


def test_message_send_requires_human_confirmation():
    assert check_tool("send_message")["verdict"] == "requires_human_confirmation"
    assert check_tool("send_email")["verdict"] == "denied"


def test_trust_tier_mapping_fails_closed():
    """写入者身份 → 信任级；无法识别的来源按最低信任处理。"""
    from app.modules.ontology.service import trust_tier_of

    assert trust_tier_of("owner") == 3
    assert trust_tier_of("agent:distiller") == 2
    assert trust_tier_of("llm") == 2
    assert trust_tier_of("someone-else") == 1
    assert trust_tier_of(None) == 1


def test_agent_never_overwrites_owner_fact():
    """核心护栏：低信任写入不得覆盖高信任当前值，只能挂起为争议。"""
    from app.modules.ontology.service import decide_write

    current = {"same_value": False, "trust_tier": 3, "valid_from": "2026-01-01T00:00:00+00:00"}
    out = decide_write(current, {"trust_tier": 2, "valid_from": "2026-02-01T00:00:00+00:00"})
    assert out["action"] == "contested"
    assert out["close_current"] is False


def test_owner_supersedes_agent_fact():
    from app.modules.ontology.service import decide_write

    current = {"same_value": False, "trust_tier": 2, "valid_from": "2026-01-01T00:00:00+00:00"}
    out = decide_write(current, {"trust_tier": 3, "valid_from": "2026-02-01T00:00:00+00:00"})
    assert out["action"] == "superseded" and out["close_current"] is True


def test_backdated_observation_is_kept_as_earlier_stint():
    """回溯观察：早于当前值生效时间 → 作为更早历史段，不作废当前值。"""
    from app.modules.ontology.service import decide_write

    current = {"same_value": False, "trust_tier": 3, "valid_from": "2026-05-01T00:00:00+00:00"}
    out = decide_write(current, {"trust_tier": 3, "valid_from": "2026-01-01T00:00:00+00:00"})
    assert out["action"] == "backdated_stint"
    assert out["close_current"] is False and out["close_before"] is True


def test_same_value_is_idempotent_or_reinforcing():
    from app.modules.ontology.service import decide_write

    current = {"same_value": True, "trust_tier": 2, "valid_from": "2026-01-01T00:00:00+00:00"}
    assert decide_write(current, {"trust_tier": 2})["action"] == "duplicate"
    assert decide_write(current, {"trust_tier": 3})["action"] == "reinforced"
    assert decide_write(current, {"trust_tier": 1})["action"] == "duplicate"


def test_dimension_cardinality_defaults_to_multi():
    """单值维度才允许「新值取代旧值」；观点类多值维度默认并列，避免静默丢知识。"""
    from app.modules.ontology.service import cardinality_of

    assert cardinality_of("belongs_to_department") == "single"
    assert cardinality_of("job_title") == "single"
    assert cardinality_of("believes") == "multi"
    assert cardinality_of("participated_in") == "multi"
    assert cardinality_of("某个没见过的新谓词") == "multi"


def test_asof_accepts_naive_datetime():
    """asof 传不带时区的时间不能崩：库里 timestamptz 带偏移，naive 需按本地时区补齐。

    回归点：曾因 naive/aware 直接比较抛 TypeError，导致实体画像接口 500。
    """
    from app.modules.ontology.service import _as_dt, _pick_current, _within

    asof = _as_dt("2000-01-01T00:00:00")
    assert asof is not None and asof.tzinfo is not None
    assert _as_dt("2026-01-01T00:00:00+08:00").tzinfo is not None

    series = [
        {"id": "a", "status": "active", "valid_from": "2026-01-01T00:00:00",
         "valid_to": "2026-06-01T00:00:00", "_effective_to": "2026-06-01T00:00:00",
         "object_value": {"v": "旧"}},
        {"id": "b", "status": "active", "valid_from": "2026-06-01T00:00:00",
         "valid_to": None, "_effective_to": None, "object_value": {"v": "新"}},
    ]
    assert _pick_current(series, _as_dt("2026-03-01T00:00:00"))["object_value"]["v"] == "旧"
    assert _pick_current(series, _as_dt("2026-09-01T00:00:00"))["object_value"]["v"] == "新"
    assert _pick_current(series, _as_dt("2000-01-01T00:00:00")) is None
    assert _within(series[1], _as_dt("2030-01-01T00:00:00")) is True


def test_diagnostics_报告结构():
    """自检报告要能被脚本和页面同时消费：有 summary、每项 ok/detail。"""
    from app.modules.ontology import service as ontology

    matrix = ontology._check_matrix()
    assert len(matrix) == 7
    assert all({"name", "expect", "actual", "ok"} <= set(m) for m in matrix)
    assert all(m["ok"] for m in matrix), [m for m in matrix if not m["ok"]]
    tt = ontology._check_timetravel()
    assert [t["ok"] for t in tt] == [True, True, True]


def test_multi_value_asof_只返回该时点成立的断言():
    """多值维度回看历史：每条断言各自判断当时是否成立，不能把今天的值带回去。"""
    from app.modules.ontology.service import _as_dt, _series, _within

    rows = [
        {"id": "a", "predicate": "believes", "status": "active",
         "valid_from": "2026-01-01T00:00:00", "valid_to": None},
        {"id": "b", "predicate": "believes", "status": "active",
         "valid_from": "2026-11-01T00:00:00", "valid_to": None},
    ]
    series = _series(rows, infer=False)          # 多值维度不互相充当边界
    asof_early = _as_dt("2026-03-01T00:00:00")
    asof_now = _as_dt("2026-12-01T00:00:00")
    assert [r["id"] for r in series if _within(r, asof_early)] == ["a"]
    assert [r["id"] for r in series if _within(r, asof_now)] == ["a", "b"]
    assert [r["id"] for r in series if _within(r, _as_dt("2020-01-01T00:00:00"))] == []


def test_review_outcome_区分真正发布与重复佐证争议():
    """回归：旧实现 action != skipped 就算发布成功，duplicate/contested 都误报已发布。

    事实列表只展示 active，contested 事实根本看不到；审核后 claim 却变 approved，
    用户就会遇到「转为事实不生效」。
    """
    from app.api.v1.knowledge import _review_outcome

    for action in ("inserted", "superseded", "backdated_stint"):
        r = _review_outcome({"action": action, "status": "active", "version": 2})
        assert r["fact_published"] is True, action
        assert r["needs_conflict_review"] is False

    for action in ("duplicate", "reinforced"):
        r = _review_outcome({"action": action, "status": "active"})
        assert r["fact_published"] is False, action
        assert "重复佐证" in r["message"]

    r = _review_outcome({"action": "contested", "status": "contested"})
    assert r["fact_published"] is False and r["needs_conflict_review"] is True
    assert "争议" in r["message"]

    r = _review_outcome({"action": "skipped", "reason": "predicate 不能为空"})
    assert r["fact_published"] is False and r["message"] == "predicate 不能为空"
    assert _review_outcome(None)["fact_published"] is False


def test_knowledge_list_limit_返回全量而非截断():
    """回归：默认 50 / 上限 200 会让前端分页条永远停在「共 50 条」。"""
    from app.api.v1.knowledge import _clamp_limit

    assert _clamp_limit(2000) == 2000
    assert _clamp_limit("500") == 500
    assert _clamp_limit(-1) == 1000        # 非法值回落到全量默认
    assert _clamp_limit("bad") == 1000
    assert _clamp_limit(10 ** 9) == 5000   # 仍拒绝无限取数


def test_withdrawal_note_留痕字段完整():
    """撤销发布必须留下「谁 / 何时 / 为什么」，reason 有界且不为空。"""
    from app.modules.ontology.service import _withdrawal_note

    n = _withdrawal_note("owner", "  误发布  ")
    assert n["withdrawn_by"] == "owner"
    assert n["withdrawal"] == "unpublished"
    assert n["reason"] == "误发布"
    assert n["at"] and "T" in n["at"]
    assert _withdrawal_note("owner", "")["reason"]      # 空理由也要有默认说明
    assert len(_withdrawal_note("owner", "x" * 500)["reason"]) == 300


def test_fact_status_clause_白名单外回落active():
    """status 不得把任意字符串带进 SQL；withdrawn 是撤销留痕的查询视图。"""
    from app.api.v1.knowledge import _fact_status_clause

    assert _fact_status_clause("withdrawn") == ("f.status = :st", {"st": "withdrawn"})
    assert _fact_status_clause("contested")[1]["st"] == "contested"
    assert _fact_status_clause("active")[1]["st"] == "active"
    assert _fact_status_clause("'; DROP TABLE knowledge.facts;--")[1]["st"] == "active"


def test_withdrawn_fact_不进时间线但可留痕():
    """撤销后：asof 时间旅行不再把该行当成立断言，但行本身仍可追溯。"""
    from app.modules.ontology.service import (
        FACT_ACTIVE, FACT_WITHDRAWN, _pick_current, _series, _timeline_rows,
    )

    group = [
        {"id": "live", "status": FACT_ACTIVE,
         "valid_from": "2026-01-01T00:00:00", "valid_to": None,
         "object_value": {"v": "当前值"}},
        {"id": "gone", "status": FACT_WITHDRAWN,
         "valid_from": "2026-06-01T00:00:00", "valid_to": "2026-11-12T00:00:00",
         "object_value": {"v": "误发布"}},
    ]
    series = _series(_timeline_rows(group), infer=False)
    assert [r["id"] for r in series] == ["live"]
    assert _pick_current(series, None)["object_value"]["v"] == "当前值"
    assert [r["id"] for r in group if r["status"] == FACT_WITHDRAWN] == ["gone"]


def test_decision_link_checks_孤立演示数据不拖累真实链路():
    """回归：12 条决策里 1 条是 seed_demo 造的孤立演示数据（无证据/无快照）。

    旧判定用「全库决策数」做分母，一条演示数据就让「决策落库 / 审计快照」
    永久 false。新判定把它显式分类并公开数量；但真实决策缺证据/缺快照
    仍必须如实报 false，不能靠改口径刷绿。
    """
    from app.api.v1.lineage import _decision_link_checks

    base = dict(decisions=12, decisions_with_evidence=11,
                decisions_with_snapshot=11, orphan_decisions=1)
    checks, detail = _decision_link_checks(base)
    assert checks == {"decisions_persisted": True, "decisions_with_snapshot": True}
    assert detail == {"linked_decisions": 11, "orphan_decisions": 1}

    assert _decision_link_checks({**base, "decisions_with_evidence": 10})[0] == {
        "decisions_persisted": False, "decisions_with_snapshot": True}
    assert _decision_link_checks({**base, "decisions_with_snapshot": 10})[0] == {
        "decisions_persisted": True, "decisions_with_snapshot": False}

    full = dict(decisions=12, decisions_with_evidence=12,
                decisions_with_snapshot=12, orphan_decisions=0)
    assert _decision_link_checks(full)[0] == {
        "decisions_persisted": True, "decisions_with_snapshot": True}
    assert _decision_link_checks(full)[1] == {
        "linked_decisions": 12, "orphan_decisions": 0}
    empty = dict(decisions=0, decisions_with_evidence=0,
                 decisions_with_snapshot=0, orphan_decisions=0)
    assert _decision_link_checks(empty)[0] == {
        "decisions_persisted": False, "decisions_with_snapshot": False}

    # 脏数据护栏：orphan 不得超过总决策数，linked 不得为负
    assert _decision_link_checks({**base, "orphan_decisions": 99})[1]["linked_decisions"] == 0


def test_needs_review_原因分类与处置建议齐全():
    """回归：待复核原因必须可诊断、有责任人/位置/步骤/验证口径。"""
    from app.modules.distillation.review import REASON_META, classify

    for code, meta in REASON_META.items():
        d = classify({"reason": code})
        assert d["code"] == code
        assert d["label"] and d["owner"] and d["where"] and d["steps"] and d["verify"]
        assert d["suggested_action"] in ("retry", "dismiss")
    # 重蒸被清空 error_info 后，原始原因仍可从 last_review 追溯
    assert classify({"last_review": {"original_reason": "llm_unavailable"}})["code"] \
        == "llm_unavailable"
    assert classify({"last_review": {"dismissed_reason": "no_claims"}})["code"] \
        == "no_claims"
    assert classify({"reason": "bogus"})["code"] == "unknown"
    assert classify(None)["code"] == "unknown"
    # 原文无切片时重蒸无意义，必须建议先修源头而不是无限重试
    assert classify({"reason": "no_chunks"})["retry_effective"] is False


def test_抽取部分失败不再丢弃全部成功结果():
    """回归：25 个切片里 1 个超时，旧逻辑把 24 个切片的主张一起报废。"""
    from app.modules.distillation.review import extraction_reason

    assert extraction_reason(0, 0, set()) == "no_chunks"
    assert extraction_reason(25, 12, {"llm_unavailable"}) == ""      # 有产出 → 继续发布
    assert extraction_reason(25, 0, {"llm_unavailable"}) == "llm_unavailable"
    assert extraction_reason(25, 0, {"invalid_json"}) == "invalid_json"
    # 端点故障优先，便于先修服务
    assert extraction_reason(25, 0, {"invalid_json", "llm_unavailable"}) \
        == "llm_unavailable"
    assert extraction_reason(25, 0, set()) == "no_claims"


def test_重蒸验证只有真正产出知识才算恢复():
    """回归：只把「不再报错」当修好，no_claims 会被误判为已恢复。"""
    from app.modules.distillation.review import verification_result

    assert verification_result("COMPLETED", {"published": 3}) == "recovered"
    assert verification_result("COMPLETED", {"published": 0}) == "still_failing"
    assert verification_result("NEEDS_REVIEW", {"published": 0}) == "still_failing"
    assert verification_result("FAILED", {"published": 0}) == "still_failing"
    assert verification_result("COMPLETED", None) == "still_failing"


def test_重复佐证的主张也算已发布链路但不放宽覆盖率():
    """回归：duplicate/reinforced 不新建 Fact，只挂 corroborating_claims，
    SQL 计入 approved_with_fact 后链路应为 true；但少一条真实 fact 仍须 false。"""
    from app.api.v1.lineage import _claims_to_facts_check

    assert _claims_to_facts_check({"approved_claims": 636,
                                   "approved_with_fact": 636,
                                   "approved_corroborated": 1}) is True
    assert _claims_to_facts_check({"approved_claims": 636,
                                   "approved_with_fact": 635,
                                   "approved_corroborated": 0}) is False
    assert _claims_to_facts_check({"approved_claims": 0,
                                   "approved_with_fact": 0}) is False
