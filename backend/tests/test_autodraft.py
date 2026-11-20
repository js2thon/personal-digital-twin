"""映射建议（auto-draft）回归：能从数据算出来的一律不算「人的工作」。

这里锁住三条约束：
1. 语义明确的元数据（主键 / 名称列 / 类型 / 必填 / 时间列 / 列集合）必须自动推出；
2. 敏感列按「完整词段」判定，绝不因 manager 含 age 就丢掉整条汇报关系；
3. 模型只产建议：可用则预填并标记来源，不可用则留空且默认不入库，两种情况下
   产出的 mapping 都必须能通过 validate_mapping（否则等于把人卡在报错上）。
"""
from app.modules.structured import autodraft as ad
from app.modules.structured import contracts as ct

HR_ROWS = [
    {"employee_id": "E1001", "employee_name": "张三", "dept_name": "数据管理部",
     "manager_id": "E0001", "job_title": "数据工程师", "grade": "P6",
     "status": "在职", "hire_date": "2021-03-01", "email": "z@x.com",
     "phone": "13800000000", "performance": "B+"},
    {"employee_id": "E1002", "employee_name": "李四", "dept_name": "平台部",
     "manager_id": "E1001", "job_title": "数据分析师", "grade": "P5",
     "status": "在职", "hire_date": "2022-07-15", "email": "l@x.com",
     "phone": "13900000001", "performance": "A"},
    {"employee_id": "E1003", "employee_name": "王五", "dept_name": "平台部",
     "manager_id": "E0001", "job_title": "后端工程师", "grade": "P6",
     "status": "离职", "hire_date": "2020-01-06", "email": "w@x.com",
     "phone": "13700000002", "performance": "B"},
]


def suggest(rows=None, use_llm=False, **kw):
    return ad.suggest_mapping(rows=rows if rows is not None else HR_ROWS,
                              source_ref={"system": "file", "table": "hr.csv"},
                              use_llm=use_llm, **kw)


def by_column(result):
    return {c["column"]: c for c in result["columns"]}


# ---------------------------------------------------------------- 自动推导

def test_entity_anchors_are_derived_not_asked():
    r = suggest()
    assert r["entity"]["pk_column"] == "employee_id"
    assert r["entity"]["name_column"] == "employee_name"
    assert r["entity"]["entity_type"] == "Person"
    assert r["mapping"]["entity"]["external_id_prefix"] == "file:"


def test_expect_columns_equals_file_columns():
    r = suggest()
    assert r["mapping"]["expect_columns"] == list(HR_ROWS[0].keys())


def test_required_follows_nonnull_rate():
    r = suggest()
    fields = {f["column"]: f for f in r["mapping"]["fields"]}
    assert fields["dept_name"]["required"] is True
    # 有一行 manager_id 为空时不应被判为必填
    rows = [dict(HR_ROWS[0]), dict(HR_ROWS[1], manager_id="")]
    fields = {f["column"]: f for f in suggest(rows)["mapping"]["fields"]}
    assert fields["manager_id"]["required"] is False


def test_dictionary_maps_common_hr_columns():
    cols = by_column(suggest())
    assert cols["dept_name"]["predicate"] == "department"
    assert cols["job_title"]["predicate"] == "job_title"
    assert cols["grade"]["predicate"] == "level"
    assert cols["status"]["predicate"] == "status"
    assert cols["hire_date"]["predicate"] == "hired_at"


def test_manager_column_becomes_entity_ref_relation():
    cols = by_column(suggest())
    item = cols["manager_id"]
    assert item["predicate"] == "reports_to"
    assert item["object_kind"] == "entity_ref"
    assert item["relation_type"] == "reports_to"
    assert item["target_entity_type"] == "Person"
    assert item["enabled"] is True, "汇报关系是组织事实的核心，不能默认丢掉"


def test_date_columns_bind_valid_range_and_skip_fact():
    rows = [dict(r, effective_from="2025-01-01", effective_to="") for r in HR_ROWS]
    r = suggest(rows)
    assert r["entity"]["valid_from_column"] == "effective_from"
    cols = by_column(r)
    assert cols["effective_from"]["enabled"] is False
    assert "时间列" in cols["effective_from"]["reason"]


# ---------------------------------------------------------------- 敏感列（词段判定）

def test_sensitive_uses_word_boundaries():
    cols = by_column(suggest())
    assert cols["performance"]["enabled"] is False       # 绩效默认不落库
    assert cols["performance"]["visibility"] == "restricted"
    assert cols["phone"]["enabled"] is False
    assert cols["manager_id"]["enabled"] is True         # 不因含 age 被误判


def test_short_ascii_hints_do_not_match_inside_words():
    assert ad._is_sensitive("manager_id") is False
    assert ad._is_sensitive("employee_age") is True
    assert ad._is_sensitive("home_city") is False
    assert ad._is_sensitive("id_card") is True
    assert ad._is_sensitive("workphone") is True
    assert ad._is_sensitive("身份证号") is True


def test_home_city_maps_to_location_not_restricted():
    rows = [{"employee_id": "E1", "employee_name": "张三", "home_city": "上海"},
            {"employee_id": "E2", "employee_name": "李四", "home_city": "南京"}]
    cols = by_column(suggest(rows))
    assert cols["home_city"]["predicate"] == "location"
    assert cols["home_city"]["visibility"] == "owner"


# ---------------------------------------------------------------- 模型建议（只产建议）

def test_unknown_columns_stay_out_by_default(monkeypatch):
    monkeypatch.setattr(ad, "_ask_llm", lambda *a, **k: {"error": "timeout"})
    rows = [dict(HR_ROWS[0], freelance_flag="Y"), dict(HR_ROWS[1], freelance_flag="N")]
    r = suggest(rows, use_llm=True)
    assert r["llm"]["used"] is False
    assert "freelance_flag" in r["unresolved"]
    assert by_column(r)["freelance_flag"]["enabled"] is False
    assert all(f["column"] != "freelance_flag" for f in r["mapping"]["fields"])


def test_llm_suggestions_are_adopted_but_marked(monkeypatch):
    monkeypatch.setattr(ad, "_ask_llm", lambda *a, **k: {"items": [
        {"column": "freelance_flag", "predicate": "is_freelancer",
         "object_kind": "literal", "confidence": 0.8}]})
    rows = [dict(HR_ROWS[0], freelance_flag="Y"), dict(HR_ROWS[1], freelance_flag="N")]
    r = suggest(rows, use_llm=True)
    item = by_column(r)["freelance_flag"]
    assert item["predicate"] == "is_freelancer"
    assert item["source"] == "llm"
    assert item["confidence"] == ad._LLM_CONFIDENCE
    assert item["enabled"] is True


def test_llm_output_is_sanitised(monkeypatch):
    """模型给中文/非法标识符/已被占用的谓词时，一律不采纳。"""
    monkeypatch.setattr(ad, "_ask_llm", lambda *a, **k: {"items": [
        {"column": "freelance_flag", "predicate": "是否外包"},
        {"column": "cost_center", "predicate": "department"},
        {"column": "skill_tag", "predicate": "Department "}]})
    rows = [dict(HR_ROWS[0], freelance_flag="Y", cost_center="CC1", skill_tag="SQL"),
            dict(HR_ROWS[1], freelance_flag="N", cost_center="CC2", skill_tag="Java")]
    r = suggest(rows, use_llm=True)
    cols = by_column(r)
    assert cols["freelance_flag"]["predicate"] == ""   # 非 snake_case
    assert cols["cost_center"]["predicate"] == ""      # department 已被 dept_name 占用
    assert cols["skill_tag"]["predicate"] == ""        # 大写同样视为非法


def test_duplicate_dictionary_predicate_is_flagged_not_overwritten():
    rows = [{"employee_id": "E1", "employee_name": "张三",
             "dept_name": "数据管理部", "department": "平台部"},
            {"employee_id": "E2", "employee_name": "李四",
             "dept_name": "数据管理部", "department": "平台部"}]
    cols = by_column(suggest(rows))
    enabled = [c for c in ("dept_name", "department") if cols[c]["enabled"]]
    assert len(enabled) == 1
    # 裁决：列名更直接者承担谓词（dept_name=8 < department=10），败者写明去向
    assert enabled == ["dept_name"]
    assert "更直接的列" in cols["department"]["reason"]


# ---------------------------------------------------------------- 可组合性

def test_department_name_column_is_not_taken_as_person_name():
    """dept_name 也含 name；名称列必须认强特征词，而不是先碰到谁就用谁。"""
    rows = [{"employee_id": "E1", "dept_name": "数据管理部", "employee_name": "张三"},
            {"employee_id": "E2", "dept_name": "平台部", "employee_name": "李四"},
            {"employee_id": "E3", "dept_name": "平台部", "employee_name": "王五"}]
    r = ad.suggest_mapping(rows=rows, source_ref={}, use_llm=False)
    assert r["entity"]["name_column"] == "employee_name"
    assert r["entity"]["pk_column"] == "employee_id"


def test_name_column_falls_back_to_pk_when_only_low_cardinality_text():
    rows = [{"employee_id": "E1", "dept_name": "数据管理部"},
            {"employee_id": "E2", "dept_name": "平台部"}]
    r = ad.suggest_mapping(rows=rows, source_ref={}, use_llm=False)
    assert r["entity"]["name_column"] == "employee_id"


def test_column_order_argument_restores_file_order():
    """行经 JSONB 往返后键序不可信，展示顺序以解析时的 columns 为准。"""
    rows = [{"zeta": "1", "alpha": "2", "mid": "3"},
            {"zeta": "4", "alpha": "5", "mid": "6"}]
    r = ad.suggest_mapping(rows=rows, source_ref={}, use_llm=False,
                           column_order=["alpha", "mid", "zeta"])
    assert r["mapping"]["expect_columns"] == ["alpha", "mid", "zeta"]


def test_generated_mapping_passes_validation():
    errors, clean = ct.validate_mapping(suggest()["mapping"])
    assert not errors, errors
    assert clean["fields"]


def test_empty_rows_returns_error_not_exception():
    r = ad.suggest_mapping(rows=[], source_ref={}, use_llm=False)
    assert r["error"] == "empty_rows"


def test_wide_table_sampling_keeps_memory_bounded():
    cols = [f"c{i}" for i in range(300)]
    rows = [{c: f"{c}-{i}" for c in cols} for i in range(20)]
    r = ad.suggest_mapping(rows=rows, source_ref={}, use_llm=False)
    assert r["profiled_rows"] <= 150          # 宽表自动降低采样
    assert len(r["mapping"]["expect_columns"]) == 300


def test_prefix_follows_source_system():
    r = ad.suggest_mapping(rows=HR_ROWS, source_ref={"system": "hr"}, use_llm=False)
    assert r["mapping"]["entity"]["external_id_prefix"] == "hr:"

