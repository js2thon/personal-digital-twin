"""② 本体草案生成：画像 → schema dict（与 cq_v2.registry 同构）+ 推断依据与置信度。

规则先行，LLM 只做「命名 / 中文标签 / 复用判断」的润色，且可关闭。
产物可直接交给 registry.publish（审核后）与 linkml.dumps。
"""
from __future__ import annotations

import json
import re

from .profiler import TableProfile

REVIEW_THRESHOLD = 0.8

# 英文表名同义词（本模块补充）+ cq_v2.mapping.SYNONYMS 中文同义词（单一来源，见 _merge_synonyms）
SYNONYMS = {
    "Person": ("employee", "employees", "staff", "user", "users", "person", "people",
               "member", "员工", "人员", "用户", "成员", "人"),
    "OrganizationUnit": ("dept", "department", "departments", "org", "organization",
                         "unit", "部门", "组织"),
    "Project": ("project", "projects", "项目"),
    "Document": ("document", "documents", "doc", "file", "文档"),
    "System": ("system", "systems", "app", "application", "系统"),
    "Skill": ("skill", "skills", "技能"),
}
def _merge_synonyms() -> None:
    try:
        from app.modules.ontology.cq_v2 import mapping as _m
    except Exception:  # noqa: BLE001  离线/单测环境
        return
    for canon, words in getattr(_m, "SYNONYMS", {}).items():
        SYNONYMS[canon] = tuple(dict.fromkeys(tuple(SYNONYMS.get(canon, ())) + tuple(words)))


_merge_synonyms()

# 常见列名 → 中文标签（LLM 不可用时的兜底；LLM 可覆盖）
COLUMN_LABELS = {
    "salary": "薪资", "budget": "预算", "level": "职级", "grade": "职级", "hire_date": "入职日期",
    "start_date": "开始日期", "end_date": "结束日期", "dept_name": "部门名称",
    "full_name": "姓名", "name": "名称", "project_name": "项目名称", "amount": "金额",
    "price": "价格", "qty": "数量", "quantity": "数量", "status": "状态", "gender": "性别",
    "age": "年龄", "city": "城市", "region": "区域", "score": "评分", "cost": "成本",
    "revenue": "收入", "created_at": "创建时间", "order_date": "下单日期",
}
REL_LABELS = {"works_at": "任职于", "reports_to": "汇报给", "part_of": "隶属于",
              "owned_by": "负责人", "belongs_to": "属于"}

# 列名词根 → 中文（宽表 HR 字段常见：post_cn_name / highest_educ_lvl_cn_name / dept_manager_name …）
_ROOTS = [
    ("highest_educ", "最高学历"), ("educ", "学历"), ("dept_manager", "部门负责人"), ("manager", "上级"),
    ("post", "岗位"), ("position", "职位"), ("job", "职务"), ("dept_company", "部门所属公司"),
    ("company", "公司"), ("dept", "部门"), ("department", "部门"), ("grade", "职级"), ("level", "级别"),
    ("sex", "性别"), ("gender", "性别"), ("nationality", "国籍"), ("marital", "婚姻状况"),
    ("political", "政治面貌"), ("birthdate", "出生日期"), ("birthplace", "出生地"), ("birth", "出生"),
    ("hire", "入职"), ("entry_dt", "入职日期"), ("work_location", "工作地点"), ("location", "地点"),
    ("region", "区域"), ("employment_type", "用工类型"), ("empl_class", "员工类别"),
    ("employee_type", "员工类型"), ("work_type", "工作类型"), ("officer", "干部类别"),
    ("action_reason", "异动原因"), ("action", "异动类型"), ("status", "状态"), ("emp_name", "姓名"),
    ("emplid", "工号"), ("emp_no", "工号"), ("age", "年龄"), ("city", "城市"), ("address", "地址"),
    ("country", "国家"), ("probation", "试用期"),
]


def _guess_label(col: str) -> str:
    c = col.lower()
    base = next((zh for k, zh in _ROOTS if c.startswith(k) or f"_{k}" in c), None)
    if not base:
        return col
    level = re.search(r"_lv(\d+)$", c)
    if level:
        base = f"{level.group(1)}级{base}"
    if c.endswith("_code") or c.endswith("_id"):
        return base + "编码"
    if "_en_" in c or c.endswith("_en_name"):
        return base + "（英文）"
    return base


LABELS = {"Person": "人员", "OrganizationUnit": "组织单元", "Project": "项目",
          "Document": "文档", "System": "系统", "Skill": "技能"}


def _pick_name_col(p) -> str:
    """实例显示名：优先「姓名/名称」类列，且取值几乎唯一（宽表里一级部门名也是 name 角色，但全表同值）。"""
    cands = [c for c in p.columns.values() if c.role in ("name", "text") and c.dtype == "text"]
    if not cands:
        return ""
    def score(c):
        n = c.name.lower()
        s = c.distinct_ratio * 10
        if re.search(r"(^name$|full_name|emp_name|person_name|_name$|姓名|名称)", n):
            s += 3
        if re.search(r"(emp|person|staff|user|full|姓名)", n):
            s += 4
        if re.search(r"(_en_|_code|dept|company|country|status|_lv\d)", n):
            s -= 5
        return s
    best = max(cands, key=score)
    return best.name if best.distinct_ratio >= 0.5 else ""


def _pascal(name: str) -> str:
    name = re.sub(r"(ies)$", "y", name)
    name = re.sub(r"(?<!s)s$", "", name)
    return "".join(w.capitalize() for w in re.split(r"[_\W]+", name) if w) or "Thing"


def _snake(name: str) -> str:
    return re.sub(r"[^0-9a-z_]+", "_", name.lower()).strip("_")


def _canonical_class(table: str) -> tuple[str, bool]:
    t = table.lower()
    for canon, words in SYNONYMS.items():
        if t in words or t.rstrip("s") in words:
            return canon, True
    return _pascal(table), False


def _rel_name(fk_col: str, target_cls: str) -> str:
    stem = re.sub(r"(_id|_no|_code|_key)$", "", fk_col).split("_")[0]
    special = {("manager", "Person"): "reports_to", ("leader", "Person"): "reports_to",
               ("dept", "OrganizationUnit"): "works_at",
               ("department", "OrganizationUnit"): "works_at",
               ("owner", "Person"): "owned_by", ("leader", "Person"): "reports_to",
               ("parent", "OrganizationUnit"): "part_of", ("supervisor", "Person"): "reports_to"}
    if stem == "parent":
        return "part_of"
    if (stem, target_cls) in special:
        return special[(stem, target_cls)]
    if stem and stem.lower() not in target_cls.lower():
        return f"has_{_snake(stem)}"
    return f"belongs_to_{_snake(target_cls)}"


def _range(dtype: str) -> str:
    return {"integer": "integer", "number": "decimal", "date": "date",
            "boolean": "boolean"}.get(dtype, "string")


def generate(profiles: dict[str, TableProfile], version: str = "draft",
             existing_schema: dict | None = None, llm=None,
             source_id: str = "") -> dict:
    """返回 {schema, class_map, inferences, review}。"""
    existing_types = set(((existing_schema or {}).get("entity_types") or {}).keys())
    entity_types, relation_types, attributes = {}, {}, {}
    class_map: dict[str, str] = {}
    inferences: list[dict] = []

    def infer(kind, ref, conf, why, **kw):
        inferences.append({"kind": kind, "ref": ref, "confidence": round(conf, 2),
                           "reason": why, **kw})

    # 1) 类：非关联表 → 类
    for t, p in profiles.items():
        if p.is_junction:
            continue
        cls, synonym = _canonical_class(t)
        reuse = cls in existing_types
        while cls in entity_types and class_map.get(t) != cls:
            cls += "X"
        class_map[t] = cls
        name_col = _pick_name_col(p)
        entity_types[cls] = {"label": LABELS.get(cls, p.comment or cls), "aliases": [t],
                             "parent": "", "source_table": t, "key": p.primary_key,
                             "name_column": name_col}
        conf = 0.95 if (p.primary_key and not p.pk_inferred) else 0.85 if p.primary_key else 0.6
        why = ["有声明主键" if not p.pk_inferred else ("推断主键 " + ",".join(p.primary_key)
                                                      if p.primary_key else "无主键")]
        if synonym:
            why.append(f"表名命中同义词 → {cls}")
        if reuse:
            why.append("复用 Registry 已登记类型")
            conf = max(conf, 0.9)
        infer("class", cls, conf, "；".join(why), table=t, operation="reuse" if reuse else "create")

    # 2) 关系：外键 → many_to_one；关联表 → many_to_many
    for t, p in profiles.items():
        if p.is_junction:
            a, b = p.foreign_keys[:2]
            sa, sb = class_map.get(a["ref_table"]), class_map.get(b["ref_table"])
            if sa and sb:
                rn = f"{_snake(sa)}_{_snake(sb)}" if t in (a["ref_table"], b["ref_table"]) \
                    else _snake(re.sub(r"s$", "", t))
                relation_types[rn] = {"label": f"{LABELS.get(sa, sa)}-{LABELS.get(sb, sb)}关联",
                                      "source": sa, "target": sb,
                                      "cardinality": "many_to_many", "via_table": t,
                                      "source_column": a["column"], "target_column": b["column"]}
                conf = 0.9 if a["declared"] and b["declared"] else 0.75
                infer("relation", rn, conf, f"关联表 {t}（两个外键、无业务列）", table=t)
            continue
        src = class_map[t]
        for fk in p.foreign_keys:
            tgt = class_map.get(fk["ref_table"])
            if not tgt:
                continue
            rn = _rel_name(fk["column"], tgt)
            if rn in relation_types and relation_types[rn]["source"] != src:
                rn = f"{_snake(src)}_{rn}"
            relation_types[rn] = {"label": REL_LABELS.get(rn, rn), "source": src, "target": tgt,
                                  "cardinality": "many_to_one", "column": fk["column"],
                                  "table": t}
            if fk["declared"]:
                conf, why = 0.95, f"声明外键 {t}.{fk['column']} → {fk['ref_table']}"
            else:
                conf = 0.6 + 0.3 * (fk["containment"] - 0.95) / 0.05 + (0.1 if fk["name_match"] else 0)
                why = (f"隐式外键：取值包含率 {fk['containment']:.0%}"
                       + ("，列名匹配" if fk["name_match"] else "，列名不匹配"))
            infer("relation", rn, min(conf, 0.95), why, table=t, column=fk["column"])

    # 3) 属性 / 枚举 / 度量 / 时间
    for t, p in profiles.items():
        if p.is_junction:
            continue
        cls = class_map[t]
        fk_cols = {fk["column"] for fk in p.foreign_keys}
        for c in p.columns.values():
            if c.name in fk_cols:
                continue
            key = c.name if c.name not in attributes else f"{_snake(cls)}_{c.name}"
            meta = {"range": _range(c.dtype), "owner": cls, "column": c.name, "role": c.role,
                    "label": c.comment or COLUMN_LABELS.get(c.name) or _guess_label(c.name)}
            if c.role == "enum":
                meta["enum_values"] = c.samples
            if c.role == "identifier":
                meta["identifier"] = True
            if c.comment:
                meta["description"] = c.comment
            attributes[key] = meta
            conf = 0.9 if c.null_ratio < 0.5 else 0.7
            infer("attribute", key, conf, f"{t}.{c.name}：{c.dtype}/{c.role}，空值率 {c.null_ratio:.0%}",
                  table=t, column=c.name, role=c.role)

    # 4) 将数据画像提升为业务对象定义：对象身份、展示属性、对象类别和数据源绑定。
    for table, cls in class_map.items():
        profile = profiles[table]
        obj = entity_types[cls]
        column_to_property = {
            meta["column"]: prop for prop, meta in attributes.items()
            if meta.get("owner") == cls and meta.get("column")
        }
        key_properties = [column_to_property[k] for k in profile.primary_key
                          if k in column_to_property]
        title_property = column_to_property.get(obj.get("name_column") or "")
        table_lower = table.lower()
        if re.search(r"(event|history|log|audit|activity|事件|历史|日志)", table_lower):
            object_kind = "event"
        elif re.search(r"(order|transaction|payment|invoice|approval|claim|订单|交易|付款|发票|审批)", table_lower):
            object_kind = "transaction"
        elif re.search(r"(dict|dictionary|lookup|reference|code_list|字典|码表)", table_lower):
            object_kind = "reference"
        else:
            object_kind = "entity"
        obj.update({
            "object_kind": object_kind,
            "title_property": title_property or "",
            "identity_rules": ([{
                "source_system": source_id, "key_properties": key_properties,
                "normalization": "trim", "priority": 100, "unique": True,
            }] if key_properties else []),
            "backing_sources": [{
                "source_id": source_id, "dataset": source_id, "table": table,
                "primary_key": list(profile.primary_key),
                "property_mappings": column_to_property,
                "mode": "materialized",
            }],
            "source_table": table,
        })

    schema = {"schema_version": version, "entity_types": entity_types,
              "relation_types": relation_types, "attributes": attributes,
              "interfaces": {},
              "constraints": {"max_hops": 3,
                              "single_valued": [k for k, v in attributes.items()
                                                if v.get("identifier")]},
              "validation_rules": {}, "status": "draft", "pinned_by_cq": []}
    if llm is not None:
        _llm_polish(schema, profiles, llm, inferences)
    review = [i for i in inferences if i["confidence"] < REVIEW_THRESHOLD]
    return {"schema": schema, "class_map": class_map, "inferences": inferences,
            "review": review,
            "stats": {"classes": len(entity_types), "relations": len(relation_types),
                      "attributes": len(attributes), "needs_review": len(review)}}


def _llm_polish(schema: dict, profiles: dict, llm, inferences: list) -> None:
    """只改 label / description / 建议复用，不增删结构；任何失败都静默回退规则结果。"""
    brief = {cls: {"table": m["source_table"],
                   "columns": list(profiles[m["source_table"]].columns)[:20],
                   "comment": profiles[m["source_table"]].comment}
             for cls, m in schema["entity_types"].items()}
    system = ("你是本体建模助手。只为给定的类、关系提供中文 label 与一句 description，"
              "不得新增或删除任何类/关系。输出 JSON："
              '{"classes":{"<Class>":{"label":"","description":""}},'
              '"relations":{"<rel>":{"label":""}},"attributes":{"<attr>":{"label":""}}}')
    try:
        data = llm.chat_json(system, json.dumps({"classes": brief,
                                                 "relations": list(schema["relation_types"]),
                                                 "attributes": {k: v.get("label") for k, v in
                                                                schema["attributes"].items()}},
                                                ensure_ascii=False), timeout=60)
    except Exception as exc:  # noqa: BLE001
        inferences.append({"kind": "llm", "ref": "polish", "confidence": 1.0,
                           "reason": f"LLM 润色跳过：{type(exc).__name__}"})
        return
    for cls, v in ((data or {}).get("classes") or {}).items():
        if cls in schema["entity_types"] and isinstance(v, dict):
            schema["entity_types"][cls]["label"] = v.get("label") or schema["entity_types"][cls]["label"]
            if v.get("description"):
                schema["entity_types"][cls]["description"] = v["description"]
    for an, v in ((data or {}).get("attributes") or {}).items():
        if an in schema["attributes"] and isinstance(v, dict) and v.get("label"):
            schema["attributes"][an]["label"] = v["label"]
    for rn, v in ((data or {}).get("relations") or {}).items():
        if rn in schema["relation_types"] and isinstance(v, dict) and v.get("label"):
            schema["relation_types"][rn]["label"] = v["label"]
