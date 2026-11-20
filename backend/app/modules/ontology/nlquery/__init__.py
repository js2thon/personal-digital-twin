"""本体智能问数（NL→Query）：自然语言问题 → 本体 IR 语义推导 → 参数化查询 → 答案。

与「设计助手问本体结构」不同，本模块回答**业务数据问题**（如「在职人数多少」）。

═══════════════════════════════════════════════════════════════════
核心改动（v2，本体推导版）：答案必须由本体语义推导，不得直接查原始库。
═══════════════════════════════════════════════════════════════════
旧版（v1）把 predicate='status' / value='A' / entity_type='Person' 硬编码在
METRIC_DEFS 里，answer() 不接收 spec——那本质是「查原始库 + 贴一段口径说明」，
无法回答「答案是怎么从本体推出来的」。v2 改为严格三段链路：

    用户 spec（建模产物，前端画布提交）
      → compiler.compile_spec(spec) 得到 IR
      → IR.semantics（本体里声明的「语义→事实库字段」映射）
      → 问句语义命中 IR.meanings → 生成参数化 SQL → 真实库

设计铁律：
1. **本体是唯一口径来源**：过滤谓词、取值、实体类型全部来自 IR.semantics；
   问句里的中文语义（如「在职」）必须能在本体 `value_meaning` 里找到才算可推导。
2. **缺语义即拒答**：本体没传 / 没编译出语义 / 问句语义在本体里没有对应映射 →
   ok=false 并说明原因，**绝不回退硬编码、绝不查库猜一个数字**。
3. **答案可追溯（provenance）**：每个答案都带 ir_hash + 命中的本体节点 id + 字段来源，
   可逐项核对「这个过滤条件来自本体哪个节点」。
4. **不编造数据**：数字只来自真实库，查询失败如实返回，不填默认值。
5. **权限不绕过**：套 policy.read_clause（AC-01），principal 由调用方从
   request.state.principal 注入，不在模块内自造身份。
6. **参数化 SQL**：过滤值走绑定参数，问题文本只用于语义匹配，不拼进 SQL。

「在职人数 = 27」的推导前提（如实标注，不是本模块硬编码）：
- 本体建模产物里 Employee.status 节点声明 value_meaning=在职 / fact_predicate=status /
  fact_value=A / fact_entity_type=Person（这些字段来自 spec，用户可在画布编辑）；
- 真实库 knowledge.facts 中 status='A' 且 active 的去重 subject 恰好 27。
若本体未声明上述语义，本模块会拒答，而不是给出 27。
"""
from __future__ import annotations

import re
import json
from typing import Any

from app.db import session as db
from app import policy

# ---------------------------------------------------------------- 问句意图（只认语义词，不认物理字段）
# patterns 只负责把问句归类为「按某语义标签计数」或「对象总数」，不含任何
# predicate / value / entity_type 硬编码——那些必须从本体 IR 推导。
INTENTS: list[dict[str, Any]] = [
    {
        "id": "semantic_count",
        "label_suffix": "人数",
        "patterns": [r"在职.{0,4}(?:人数|员工|多少|有几|多少人多吗)", r"(?:人数|员工).{0,4}在职",
                     r"active\s*headcount"],
        "semantic_hint": "在职",   # 用于在 IR.meanings 里按 value_meaning 命中
        "aggregate": "count_distinct_subject",   # 聚合方式属于「问法」而非「本体口径」
    },
    {
        "id": "total_count",
        "label_suffix": "总数",
        # 收紧：对象词必须是「员工/人员/职工」等明确词，不接受裸「人」当主语，
        # 否则「已婚人数」「张三人有多少」会被误判成员工总数（已实测踩坑）。
        "patterns": [r"(?:员工|人员|职工|雇员).{0,3}(?:总数|人数|多少|有几)",
                     r"多少(?:员工|人员|职工)", r"有多少人(?:员|吗|？|\?|$)",
                     r"headcount"],
        "semantic_hint": "",       # 总数：只需对象类型，不需状态语义
        "aggregate": "count_distinct_subject_any",
    },
    {
        "id": "entity_lookup",
        "label_suffix": "",
        # 实体检索：「X 是谁 / 谁负责 X / X 的负责人」。返回属性值（人名）而非计数。
        # 只负责把问句归类为检索，具体映射哪个属性由本体 IR.lookups 的 value_meaning 决定。
        "patterns": [r"负责人.{0,6}(?:是谁|是哪些|有哪些|谁|叫什么|哪些人)",
                     r"谁.{0,4}(?:负责|是.{0,4}负责人)", r"谁是.{0,6}负责人",
                     r"负责人\s*$", r"部门负责人"],
        "semantic_hint": "部门负责人",   # 在 IR.lookups 里按 value_meaning 命中（可含/被含）
        "aggregate": "distinct_values",
    },
]

# 问句里出现的对象词 → 用于在本体 entity_alias_map 里查物理类型
_OBJECT_WORDS = ("员工", "人员", "职工", "雇员", "人", "项目", "部门", "组织")


def _match_intent(question: str) -> dict[str, Any] | None:
    q = str(question or "").strip()
    for it in INTENTS:
        if any(re.search(p, q, re.I) for p in it["patterns"]):
            return it
    return None


def parse_question(question: str) -> dict[str, Any]:
    """把问句解析成「意图 + 语义提示 + 对象词」（纯函数，不含物理字段）。"""
    q = str(question or "").strip()
    intent = _match_intent(q)
    # 部门限定词（如「研发部负责人是谁」的「研发部」）：只记录，不硬做过滤——
    # 是否可按部门过滤取决于本体是否声明部门→负责人语义，交由 derive_query/answer 如实处理。
    dept_m = re.search(r"([\u4e00-\u9fa5]{2,10}(?:部|处|组|中心|团队|科室))\s*(?:的)?\s*负责人", q)
    return {
        "question": q,
        "intent": intent["id"] if intent else "",
        "intent_obj": intent,
        "semantic_hint": intent["semantic_hint"] if intent else "",
        "object_words": [w for w in _OBJECT_WORDS if w in q],
        "department_qualifier": dept_m.group(1) if dept_m else "",
        "understood": bool(intent),
        "reason": "" if intent else "未识别到可回答的业务问题；当前支持：在职人数、员工总数、部门负责人是谁",
    }


# ---------------------------------------------------------------- 本体声明的实体类型 ↔ 库里真实类型
def entity_types_in_library() -> list[str]:
    """库里真实存在的实体类型清单（ontology.entities.entity_type 去重）。

    问数口径核对用：本体里写的 fact_entity_type 必须是这里的某一个，否则
    `e.entity_type = :m_etype` 永远匹配不上、答案会静默变成 0（见 resolve_entity_type）。
    查不到（库/表不可达）返回 []，调用方按「库不可知」处理，不阻断问数。
    """
    try:
        rows = db.fetch_all(
            "SELECT entity_type, COUNT(*) AS n FROM ontology.entities"
            " WHERE entity_type IS NOT NULL AND entity_type <> ''"
            " GROUP BY entity_type ORDER BY n DESC, entity_type ASC", {})
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for r in rows or []:
        t = str(r.get("entity_type") or "").strip()
        if t and t not in out:
            out.append(t)
    return out


def resolve_entity_type(declared: str, catalog: list[str]) -> tuple[str, str]:
    """把本体声明的实体类型对到库里的真实写法。纯函数，零 I/O。

    返回 (canonical, note)：
      · 精确命中            → (declared, "")
      · 只差大小写/空白且唯一 → (库里的写法, 校正说明)
      · 库里根本没有该类型   → ("", 说明 + 现有类型清单)   ← 调用方必须据此拒答
      · catalog 为空（库不可知）→ (declared, "")，不阻断

    为什么不是「对不上就当 0」：`WHERE e.entity_type = 'employee'` 匹配不到
    'Person' 时，COUNT 返回 0——那是「库里真的没有这样的人」的意思，不是
    「本体口径写错了」。两者必须区分，否则用户会拿到一个看起来正常、实际错误的 0。
    """
    d = str(declared or "").strip()
    if not d:
        return "", "本体未声明实体类型"
    if not catalog:
        return d, ""          # 库不可知：照声明走，不在代码里替用户改口径
    if d in catalog:
        return d, ""
    fold = d.casefold()
    hits = [c for c in catalog if c.casefold() == fold]
    if len(hits) == 1:
        return hits[0], (f"本体声明的实体类型「{d}」在库里实际写作「{hits[0]}」，"
                         f"已按忽略大小写匹配；建议把对象的「事实实体类型」改成 {hits[0]}")
    known = "、".join(catalog)
    return "", (f"库里没有实体类型「{d}」，问数无法定位实体，已拒答（不返回 0 冒充答案）。"
                f"现有实体类型：{known}。请在本体对象详情里把「事实实体类型"
                f"（fact_entity_type）」改成上述之一")


def entity_types_for_predicates(predicates: list[str]) -> list[dict[str, Any]]:
    """反查：带这些谓词的事实，实际挂在库里的哪些实体类型上（按主体数降序）。

    用途：本体把 fact_entity_type 写错（如 employee）而库里只有 Person 时，别把
    用户卡在「手改成什么」——用事实数据本身定位。谓词是本体的声明（status=A），
    实体类型由数据给出，属推导而非猜。返回 [] 表示查不到/库不可达，调用方按
    「仍无法定位」处理。
    """
    preds: list[str] = []
    for p in predicates or []:
        s = str(p or "").strip()
        if s and s not in preds:
            preds.append(s)
    if not preds:
        return []
    binds = {f"g{i}": p for i, p in enumerate(preds)}
    ph = ", ".join(f":g{i}" for i in range(len(preds)))
    sql = (
        "SELECT e.entity_type, COUNT(DISTINCT f.subject_id) AS n"
        " FROM knowledge.facts f JOIN ontology.entities e ON e.id = f.subject_id"
        " WHERE f.status = 'active' AND f.predicate IN (" + ph + ")"
        " AND e.entity_type IS NOT NULL AND e.entity_type <> ''"
        " GROUP BY e.entity_type ORDER BY n DESC, e.entity_type ASC"
    )
    try:
        rows = db.fetch_all(sql, binds)
    except Exception:  # noqa: BLE001
        return []
    return [{"entity_type": str(r.get("entity_type") or "").strip(),
             "subjects": int(r.get("n") or 0)}
            for r in rows or [] if str(r.get("entity_type") or "").strip()]


def _plan_predicates(plan: dict[str, Any]) -> list[str]:
    """计划里用到的谓词（过滤条件 + 检索属性），用于反查真实实体类型。"""
    out = [str(f.get("predicate") or "") for f in (plan.get("filters") or [])]
    lk = plan.get("lookup") or {}
    if lk.get("predicate"):
        out.append(str(lk["predicate"]))
    return [p for p in out if p]


# ---------------------------------------------------------------- 从本体 IR 推导查询
class OntologyNotQueryable(ValueError):
    """本体无法推导出该问题的查询（缺 spec / 缺语义 / 语义未映射）。"""


def derive_query(ir: dict[str, Any] | None, parsed: dict[str, Any]) -> dict[str, Any]:
    """从 IR.semantics 推导参数化查询计划 + provenance。纯函数，零 I/O。

    抛 OntologyNotQueryable 表示本体不支持该问法（调用方据此如实拒答）。
    """
    if not ir:
        raise OntologyNotQueryable("未提供本体（spec），无法从本体推导答案")
    sem = ir.get("semantics") or {}
    alias_map = sem.get("entity_alias_map") or {}
    etype_map = sem.get("entity_types") or {}
    meanings = sem.get("meanings") or []
    lookups = sem.get("lookups") or []
    if not alias_map and not meanings and not lookups:
        raise OntologyNotQueryable("本体未声明任何「语义→事实库字段」映射，无法推导；"
                                   "请先在画布对象上填 fact_entity_type、属性上填 "
                                   "value_meaning/fact_predicate/fact_value")

    intent = parsed["intent_obj"]
    hint = parsed.get("semantic_hint", "")
    plan_filters: list[dict[str, str]] = []
    provenance: list[dict[str, str]] = []

    # 实体检索（如「部门负责人是谁」）：必须在本体 lookups 里按 value_meaning 命中。
    # lookups 是检索类语义（有 value_meaning + fact_predicate、无 fact_value），
    # 返回事实里的属性值（人名等），区别于 meanings 的过滤计数。
    if intent["id"] == "entity_lookup":
        lk = None
        for m in lookups:
            mm, hh = m.get("meaning", ""), hint
            if hh and (hh in mm or mm in hh):
                lk = m
                break
        if not lk:
            known = "、".join(sorted({m.get("meaning", "") for m in lookups})) or "（无）"
            raise OntologyNotQueryable(
                f"本体里没有「{hint}」对应的检索语义；当前本体可检索的属性语义：{known}")
        if not lk.get("entity_type"):
            raise OntologyNotQueryable(
                f"本体属性「{lk.get('attribute')}」所属对象未声明 fact_entity_type，无法定位实体类型")
        provenance.append({
            "role": "检索属性", "from_node": lk["node_id"],
            "from_fields": "value_meaning/fact_predicate（无 fact_value）",
            "value": f"{lk['meaning']} → 取 {lk['predicate']} 的属性值",
        })
        provenance.append({
            "role": "实体类型", "from_node": "semantics.entity_types",
            "from_fields": "fact_entity_type", "value": lk["entity_type"],
        })
        return {
            "intent": intent["id"], "label": lk["meaning"],
            "entity_type": lk["entity_type"], "filters": [],
            "object_name": lk.get("object", ""),
            "aggregate": intent["aggregate"],
            "lookup": {"predicate": lk["predicate"], "meaning": lk["meaning"],
                       "attribute": lk["attribute"]},
            "provenance": provenance,
            "ir_hash": ir.get("hash", ""),
            "semantics_ready": bool(sem.get("ready")),
        }

    # 语义计数（如「在职人数」）：必须在本体 meanings 里按 value_meaning 命中
    if intent["id"] == "semantic_count":
        hit = None
        for m in meanings:
            if hint and (hint in m.get("meaning", "") or m.get("meaning", "") in hint):
                hit = m
                break
        if not hit:
            known = "、".join(sorted({m.get("meaning", "") for m in meanings})) or "（无）"
            raise OntologyNotQueryable(
                f"本体里没有「{hint}」对应的语义映射；当前本体可查询的语义：{known}")
        if not hit.get("entity_type"):
            raise OntologyNotQueryable(
                f"本体属性「{hit.get('attribute')}」所属对象未声明 fact_entity_type，无法定位实体类型")
        plan_filters.append({"predicate": hit["predicate"], "value": hit["fact_value"]})
        provenance.append({
            "role": "过滤条件", "from_node": hit["node_id"],
            "from_fields": "value_meaning/fact_predicate/fact_value",
            "value": f"{hit['meaning']} → {hit['predicate']}={hit['fact_value']}",
        })
        etype = hit["entity_type"]
        etype_src = hit["source"]
        obj_name = hit.get("object", "")
        label = f"{hint}{intent['label_suffix']}"
    else:
        # 总数：按对象词在本体 alias_map 里找物理类型
        etype = ""
        obj_name = ""
        label_word = "员工"
        for w in parsed.get("object_words", []):
            if w in alias_map:
                etype = alias_map[w]
                label_word = w
                break
        if not etype:
            known = "、".join(sorted(alias_map)) or "（无）"
            raise OntologyNotQueryable(
                f"本体里找不到问题中的对象；当前本体已映射的对象别名：{known}")
        obj_name = next((k for k, v in etype_map.items() if v == etype), "")
        provenance.append({
            "role": "实体类型", "from_node": "semantics.entity_alias_map",
            "from_fields": "fact_entity_type/aliases", "value": etype,
        })
        label = f"{label_word}{intent['label_suffix']}"

    return {
        "intent": intent["id"], "label": label,
        "entity_type": etype, "filters": plan_filters,
        "object_name": obj_name,
        "aggregate": intent["aggregate"],
        "provenance": provenance,
        "ir_hash": ir.get("hash", ""),
        "semantics_ready": bool(sem.get("ready")),
    }


# ---------------------------------------------------------------- 执行（参数化 + 权限）
def _build_sql(plan: dict[str, Any], principal: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """按本体推导出的 plan 生成参数化查询：事实表 + 权限逐点过滤 + 语义过滤 + 去重计数。"""
    binds: dict[str, Any] = {}
    # 实体检索：按本体命中的 predicate 取属性值，按值去重并给管辖人数。
    # 多对一语义（多个员工共享一个负责人）——按属性值分组，不冒充「唯一一条」。
    if plan["intent"] == "entity_lookup":
        lk = plan["lookup"]
        binds["lk_pred"] = str(lk["predicate"])
        binds["lk_etype"] = str(plan["entity_type"])
        acl = policy.read_clause("f", principal, binds)
        sql = (
            "SELECT f.object_value->>'value' AS value, "
            "  COUNT(DISTINCT f.subject_id) AS n"
            " FROM knowledge.facts f"
            " LEFT JOIN ontology.entities e ON e.id = f.subject_id"
            " WHERE f.status = 'active' AND " + acl
            + " AND e.entity_type = :lk_etype AND f.predicate = :lk_pred"
            + " AND f.object_value->>'value' IS NOT NULL AND f.object_value->>'value' <> ''"
            + " GROUP BY f.object_value->>'value' ORDER BY n DESC, value ASC"
        )
        return sql, binds
    pred_clauses: list[str] = []
    for i, flt in enumerate(plan["filters"]):
        pk, vk = f"m_pred_{i}", f"m_val_{i}"
        binds[pk], binds[vk] = str(flt["predicate"]), str(flt["value"])
        pred_clauses.append(
            "EXISTS (SELECT 1 FROM knowledge.facts mf WHERE mf.subject_id = f.subject_id"
            " AND mf.predicate = :" + pk + " AND mf.object_value->>'value' = :" + vk
            + " AND mf.status = 'active')")
    binds["m_etype"] = str(plan["entity_type"])
    extra = (" AND " + " AND ".join(pred_clauses)) if pred_clauses else ""
    acl = policy.read_clause("f", principal, binds)
    sql = (
        "SELECT COUNT(DISTINCT f.subject_id) AS value, COUNT(*) AS fact_count"
        " FROM knowledge.facts f"
        " LEFT JOIN ontology.entities e ON e.id = f.subject_id"
        " WHERE f.status = 'active' AND " + acl
        + " AND e.entity_type = :m_etype" + extra
    )
    return sql, binds


def _evidence(plan: dict[str, Any], principal: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """取支撑答案的样本事实（按本体推导的过滤条件，且套同一套权限）。"""
    if not plan["filters"]:
        return []  # 总数类不展示误导性样本
    flt = plan["filters"][0]
    binds: dict[str, Any] = {"m_pred": str(flt["predicate"]), "m_val": str(flt["value"]),
                             "m_etype": str(plan["entity_type"]), "m_lim": limit}
    acl = policy.read_clause("f", principal, binds)
    sql = (
        "SELECT f.subject_id, e.canonical_name, f.predicate, f.object_value, f.status"
        " FROM knowledge.facts f LEFT JOIN ontology.entities e ON e.id = f.subject_id"
        " WHERE f.status = 'active' AND " + acl + " AND e.entity_type = :m_etype"
        + " AND f.predicate = :m_pred AND f.object_value->>'value' = :m_val"
        + " ORDER BY f.recorded_at DESC NULLS LAST LIMIT :m_lim"
    )
    try:
        rows = db.fetch_all(sql, binds)
    except Exception:
        return []
    return [{
        "subject_id": str(r.get("subject_id", "")),
        "name": r.get("canonical_name") or "",
        "predicate": r.get("predicate", ""),
        "value": r.get("object_value"),
    } for r in rows]


def answer(question: str, *, spec: dict[str, Any] | None = None,
           principal: dict[str, Any] | None = None,
           with_evidence: bool = True) -> dict[str, Any]:
    """回答业务数据问题。**必须传本体 spec**；答案由本体语义推导，缺语义即拒答。

    principal 必须由调用方从 AC-01（request.state.principal）注入。
    """
    q = str(question or "").strip()
    parsed = parse_question(q)
    if not parsed["understood"]:
        return {"ok": False, "understood": False, "answer": "", "reason": parsed["reason"],
                "question": q, "derivation": "none"}

    # 编译本体（唯一口径来源）
    from app.modules.ontology.runtime.compiler import compile_spec
    try:
        ir = compile_spec(spec or {})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "understood": True, "answer": "",
                "reason": f"本体编译失败：{type(exc).__name__}: {exc}",
                "question": q, "derivation": "none"}

    # 从本体 IR 推导查询计划；推不出来就如实拒答，绝不查库
    try:
        plan = derive_query(ir, parsed)
    except OntologyNotQueryable as exc:
        return {
            "ok": False, "understood": True, "answer": "",
            "reason": str(exc), "question": q, "derivation": "ontology-refused",
            "provenance": [], "ir_hash": ir.get("hash", ""),
            "ontology_health": ir.get("health", {}).get("semantics", {}),
        }

    # 实体类型校准：本体写的 fact_entity_type 必须能在库里对上。
    # 对不上（如写成 employee、库里是 Person）时，SQL 会静默返回 0——那是「库里没有」
    # 的语义，不是「口径写错」的语义，必须拒答并指出真实类型，不能给个假 0。
    declared = str(plan.get("entity_type") or "").strip()
    catalog = entity_types_in_library()
    canon, type_note = resolve_entity_type(declared, catalog)
    fix: dict[str, Any] | None = None
    # 出错时前端要能一键写回「事实实体类型」，故先算好目标对象（节点 id + 名称）
    obj_name = str(plan.get("object_name") or "")
    obj_id = next((str(o.get("id") or "") for o in (ir.get("objects") or [])
                   if str(o.get("name") or "") == obj_name), "")
    if declared and catalog and not canon:
        # 库里确实没有这个类型：别急着让用户手改——用「本体声明的谓词」反查事实数据，
        # 看这些谓词实际挂在哪个实体类型上。唯一命中就按数据校正后作答（provenance 注明
        # 「按谓词反查」，不是猜）；多个命中说明数据本身分属多类，无法唯一确定 → 拒答并列候选。
        grounded = entity_types_for_predicates(_plan_predicates(plan))
        preds = "、".join(_plan_predicates(plan)) or "（无）"
        if len(grounded) == 1:
            canon = grounded[0]["entity_type"]
            type_note = (f"本体声明的实体类型「{declared}」在库里不存在；库里谓词 {preds} 的"
                         f"事实全部属于「{canon}」（{grounded[0]['subjects']} 个主体），"
                         f"已按事实数据校正后作答。建议把对象的「事实实体类型」改成 {canon}")
            plan = dict(plan)
            plan["provenance"] = list(plan.get("provenance") or []) + [{
                "role": "实体类型校正",
                "from_node": "knowledge.facts / ontology.entities",
                "from_fields": "按谓词反查真实 entity_type",
                "value": f"{declared} → {canon}",
            }]
        else:
            cands = [g["entity_type"] for g in grounded]
            reason = (type_note if not cands else
                      f"库里没有实体类型「{declared}」；谓词 {preds} 的事实分别属于 "
                      f"{'、'.join(cands)}，无法唯一确定实体类型，已拒答（不返回 0 冒充答案）。"
                      f"请把对象的「事实实体类型（fact_entity_type）」改成其中之一")
            return {
                "ok": False, "understood": True, "answer": "",
                "reason": reason, "question": q, "derivation": "ontology-type-mismatch",
                "provenance": plan["provenance"], "ir_hash": plan["ir_hash"],
                "planned_entity_type": declared, "library_entity_types": catalog,
                "suggested_entity_types": cands,
                "object": obj_name, "node_id": obj_id,
                "ontology_health": ir.get("health", {}).get("semantics", {}),
            }
    if canon and canon != declared:
        plan = dict(plan)
        plan["entity_type"] = canon
        if not fix:   # 大小写校正（数据反查那条已自己加过 provenance）
            plan["provenance"] = list(plan.get("provenance") or []) + [{
                "role": "实体类型校正", "from_node": "ontology.entities.entity_type",
                "from_fields": "库里真实类型（忽略大小写匹配）",
                "value": f"{declared} → {canon}",
            }]
        # 一键修复载荷：前端据此在对象详情里写回「事实实体类型」，改完下次不再校正
        fix = {"kind": "set_fact_entity_type", "object": obj_name, "node_id": obj_id,
               "from": declared, "to": canon}

    # principal：AC-01 注入的已归一化 dict 直接用，否则归一化
    if not (isinstance(principal, dict) and "role" in principal and "scope" in principal):
        principal = policy.principal_from(principal)
    sql, binds = _build_sql(plan, principal)
    try:
        rows = db.fetch_all(sql, binds)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False, "understood": True, "answer": "",
            "reason": f"查询失败：{type(exc).__name__}: {exc}",
            "question": q, "derivation": "query-failed",
            "provenance": plan["provenance"], "ir_hash": plan["ir_hash"],
            "entity_type_note": type_note, "fix": fix,
        }
    # 实体检索：把结果列成人名 + 管辖人数（多对一，按属性值去重）
    if plan["intent"] == "entity_lookup":
        items = [{"name": str(r.get("value") or ""),
                  "count": int(r.get("n") or 0)} for r in rows]
        items = [x for x in items if x["name"]]
        phase = "、".join(f"{x['name']}（{x['count']}人）" for x in items)
        answer_text = f"{plan['label']}：{phase}" if items else f"{plan['label']}：暂无数据"
        # 部门限定：本体未声明「部门→负责人」过滤语义，不能假装按部门筛过——如实标注。
        dq = parsed.get("department_qualifier", "")
        dept_note = (f"问句含部门限定「{dq}」，但本体未声明部门→负责人过滤语义，"
                     f"以下为全部{plan['label']}，未按部门筛选") if dq else ""
        if not items:
            return {
                "ok": True, "understood": True, "answer": answer_text,
                "intent": plan["intent"],
                "entities": [], "question": q, "derivation": "ontology-derived",
                "ir_hash": plan["ir_hash"], "provenance": plan["provenance"],
                "entity_type_note": type_note, "fix": fix,
                "query_plan": {"entity_type": plan["entity_type"], "filters": [],
                               "aggregate": plan["aggregate"], "lookup": plan["lookup"]},
                "evidence": [], "evidence_count": 0,
                "note": ("语义已从本体推导，但当前权限视角下没有匹配的事实数据"
                         + ("；" + dept_note if dept_note else "")),
                "source": "本体 IR.semantics 推导 → knowledge.facts（参数化查询 + AC-01 权限）",
                "ontology_health": ir.get("health", {}).get("semantics", {}),
            }
        return {
            "ok": True, "understood": True, "answer": answer_text,
            "intent": plan["intent"],
            "entities": items, "entity_count": len(items), "question": q,
            "derivation": "ontology-derived",   # 答案经本体检索类语义推导
            "ir_hash": plan["ir_hash"], "provenance": plan["provenance"],
                "entity_type_note": type_note, "fix": fix,
            "query_plan": {"entity_type": plan["entity_type"], "filters": [],
                           "aggregate": plan["aggregate"], "lookup": plan["lookup"]},
            "evidence": items, "evidence_count": len(items),
            "note": ("多对一：多个员工共享同一负责人，按负责人去重并给出管辖人数"
                     + ("；" + dept_note if dept_note else "")),
            "source": "本体 IR.lookups 推导 → knowledge.facts（参数化查询 + AC-01 权限）",
            "ontology_health": ir.get("health", {}).get("semantics", {}),
        }

    value = int(rows[0]["value"]) if rows else 0
    evidence = _evidence(plan, principal) if with_evidence else []
    return {
        "ok": True, "understood": True,
        "answer": f"{plan['label']}：{value}",
        "intent": plan["intent"],
        "value": value, "unit": "人",
        "question": q,
        "derivation": "ontology-derived",   # 明确标注：答案经本体语义推导
        "ir_hash": plan["ir_hash"],
        "provenance": plan["provenance"],
        "entity_type_note": type_note, "fix": fix,
        "query_plan": {
            "entity_type": plan["entity_type"], "filters": plan["filters"],
            "aggregate": plan["aggregate"],
        },
        "evidence": evidence, "evidence_count": len(evidence),
        "source": "本体 IR.semantics 推导 → knowledge.facts（参数化查询 + AC-01 权限）",
        "ontology_health": ir.get("health", {}).get("semantics", {}),
    }


def capabilities() -> dict[str, Any]:
    """问数能力自述：本体需要声明哪些字段、问句可问什么、库里真实有哪些实体类型。

    `entity_types` 是 ontology.entities 里的实际取值：对象上的「事实实体类型」
    必须写成这里的一个（区分大小写），否则问数会拒答并提示改成哪个——这样
    前端能直接把选项列出来，不用用户猜 Person / employee。
    """
    catalog = entity_types_in_library()
    return {
        "requires": {
            "object": ["fact_entity_type", "aliases"],
            "attribute": ["value_meaning", "fact_predicate", "fact_value（检索类可缺省）"],
            "lookup_attribute": "检索类（如部门负责人）：value_meaning + fact_predicate，"
                                "不填 fact_value 即返回属性值而非计数",
        },
        "entity_types": catalog,
        "entity_types_note": ("fact_entity_type 需与上述之一完全一致（区分大小写）；"
                              "库不可达时该清单为空，问数不做类型校准"),
        "intents": [{"id": it["id"], "patterns": it["patterns"],
                     "semantic_hint": it["semantic_hint"]} for it in INTENTS],
        "note": "口径来自本体建模产物（spec→IR.semantics），不在代码里硬编码；"
                "本体缺映射即拒答，不会查原始库猜答案",
    }


def self_check() -> dict[str, Any]:
    """纯函数自检：问句解析 + 本体推导 + 缺语义拒答 + 参数化（不含 DB）。"""
    ok = True
    spec = {
        "nodes": [
            {"id": "o", "kind": "entity", "fields": {
                "name": "Employee", "label": "员工", "primary_key": "employee_no",
                "fact_entity_type": "Person", "aliases": "员工,人员",
                "attributes": "employee_no:string\nstatus:string"}},
            {"id": "a", "kind": "entity", "fields": {
                "name": "status", "range": "string", "value_meaning": "在职",
                "fact_predicate": "status", "fact_value": "A"}},
        ],
        "edges": [{"from": "o", "to": "a"}],
    }
    from app.modules.ontology.runtime.compiler import compile_spec
    ir = compile_spec(spec)
    plan = derive_query(ir, parse_question("在职人数多少"))
    ok = ok and plan["entity_type"] == "Person"
    ok = ok and plan["filters"] == [{"predicate": "status", "value": "A"}]
    ok = ok and plan["provenance"][0]["from_node"] == "a"
    # 总数类：从本体 alias_map 推导
    plan2 = derive_query(ir, parse_question("公司有多少员工"))
    ok = ok and plan2["entity_type"] == "Person" and plan2["filters"] == []
    # 缺语义：必须拒答（不再回退硬编码）
    refused = False
    try:
        derive_query(compile_spec({}), parse_question("在职人数多少"))
    except OntologyNotQueryable:
        refused = True
    ok = ok and refused
    # 语义不在本体里：拒答
    # 先把问题改成本体会识别、但语义标签没声明的（如「退休人数」命中 total 意图前需确保
    # 它是 semantic_count）。直接构造一个 hint 无匹配的 parsed 调 derive_query。
    refused2 = False
    try:
        derive_query(ir, {"intent_obj": INTENTS[0], "intent": "semantic_count",
                          "semantic_hint": "已婚", "object_words": ["员工"]})
    except OntologyNotQueryable:
        refused2 = True
    ok = ok and refused2
    # 问句层面：无关/误导问句不应被识别为总数（避免「已婚人数」误判）
    ok = ok and not parse_question("已婚人数多少")["understood"]
    # 参数化：问题文本不进 SQL
    sql, binds = _build_sql(plan, {"tenant_id": "default", "user_id": "u"})
    ok = ok and "在职" not in sql and ":m_val_0" in sql and binds.get("m_val_0") == "A"

    # ---- 实体检索（部门负责人是谁）----
    spec2 = json.loads(json.dumps(spec))
    spec2["nodes"].append({"id": "dm", "kind": "entity", "fields": {
        "name": "dept_manager_name", "range": "string",
        "value_meaning": "部门负责人", "fact_predicate": "dept_manager_name"}})
    spec2["edges"].append({"from": "o", "to": "dm"})
    ir2 = compile_spec(spec2)
    ok = ok and any(l["meaning"] == "部门负责人" for l in ir2["semantics"]["lookups"])
    pq2 = parse_question("部门负责人是谁")
    ok = ok and pq2["intent"] == "entity_lookup" and pq2["understood"]
    plan3 = derive_query(ir2, pq2)
    ok = ok and plan3["intent"] == "entity_lookup"
    ok = ok and plan3["lookup"]["predicate"] == "dept_manager_name"
    ok = ok and plan3["provenance"][0]["from_node"] == "dm"
    sql3, binds3 = _build_sql(plan3, {"tenant_id": "default", "user_id": "u"})
    ok = ok and "部门负责人" not in sql3 and ":lk_pred" in sql3
    ok = ok and binds3.get("lk_pred") == "dept_manager_name"
    # 本体没声明检索语义时：检索问句拒答（用不含 lookups 的 ir）
    refused3 = False
    try:
        derive_query(ir, parse_question("部门负责人是谁"))
    except OntologyNotQueryable:
        refused3 = True
    ok = ok and refused3
    # ---- 实体类型校准（本体写 employee、库里是 Person：不能静默返回 0）----
    cat = ["Person", "Project", "Organization"]
    ok = ok and resolve_entity_type("Person", cat) == ("Person", "")
    c1, n1 = resolve_entity_type("employee", cat)
    ok = ok and c1 == "" and "没有实体类型" in n1 and "Person" in n1   # 大小写都不对 → 拒答
    c2, n2 = resolve_entity_type("person", cat)
    ok = ok and c2 == "Person" and "忽略大小写" in n2                 # 仅大小写差 → 校正
    c3, n3 = resolve_entity_type(" Person ", cat)
    ok = ok and c3 == "Person" and n3 == ""                          # 空白容错
    c4, n4 = resolve_entity_type("Person", [])
    ok = ok and c4 == "Person" and n4 == ""                          # 库不可知 → 不阻断
    # ---- 实体类型反查（写错类型时用事实数据定位，而不是让用户去猜）----
    # 纯函数层只锁两条契约：①从计划里取哪些谓词反查 ②空谓词必须短路不碰库。
    # 真正的库反查（status → Person/27 个主体）要连 PG，由
    # web/.runtime/verify_etype_grounded.py 覆盖，这里不连库。
    ok = ok and _plan_predicates(plan) == ["status"]              # 过滤类：取过滤谓词
    ok = ok and _plan_predicates(plan3) == ["dept_manager_name"]   # 检索类：取检索谓词
    ok = ok and _plan_predicates(plan2) == []                     # 总数类：无谓词 → 不猜
    ok = ok and _plan_predicates({"filters": [{"predicate": ""}, {}]}) == []
    ok = ok and entity_types_for_predicates([]) == []              # 短路，不查库
    # 修复载荷靠 object_name 定位本体里的对象节点，三种意图都得带（否则一键改不了）
    ok = ok and plan.get("object_name") == "Employee"
    ok = ok and plan2.get("object_name") == "Employee"
    ok = ok and plan3.get("object_name") == "Employee"
    return {"pass": bool(ok), "intents": [it["id"] for it in INTENTS],
            "entity_type_resolution": "case-sensitive-with-catalog",
            "entity_type_grounding": "predicate-reverse-lookup",
            "derivation": "ontology-only"}
