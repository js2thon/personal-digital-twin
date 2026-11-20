"""Competency Questions（CQ）：把「本体该有什么」写成可验证的问题。

设计取舍见 docs/cq-ontology-assessment.md §4，这里只重复三条落地原则：

1. **CQ 不写查询语言**。存「意图 + 主语类型 + 需要的关系/属性 + 答案形态」，
   一套定义既能翻成事实查询、图路径、时间切片，也能被业务方读懂。
2. **自动只做到「提案」**。这里不做 LLM 生成 schema；`analyze()` 只做确定性抽取，
   把问句里的名词对到已有类型、动词对到已登记关系，命不中的列出来给人看。
3. **只有「答不上」才产生缺口**。答案齐全的 CQ 不产出任何提示，
   否则缺口列表会退化成每天几百条噪声。

状态机：draft（草案，待审核）→ active（参与覆盖率与门禁）→ retired（保留历史）。
机器写入只能落在 draft，`active` 必须经过人。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from app.db import session as db
from app.modules.ontology import graph as og
from app.modules.ontology import projection as proj

INTENTS = ("lookup", "path", "temporal", "aggregate", "boolean", "compare")
INTENT_LABELS = {
    "lookup": "查询取值", "path": "关系链路", "temporal": "时点追溯",
    "aggregate": "聚合统计", "boolean": "存在判定", "compare": "对比",
}
ANSWER_SHAPES = ("entity", "value", "list", "count", "yes_no")
ANSWER_LABELS = {
    "entity": "单个实体", "value": "一个值", "list": "实体列表",
    "count": "一个数字", "yes_no": "是/否",
}
STATUSES = ("draft", "active", "retired")
STATUS_LABELS = {"draft": "待审核", "active": "已启用", "retired": "已停用"}

# 覆盖率达标线：低于它算「答不全」，只有 ≥ 它才在列表里显示为可用
ANSWER_THRESHOLD = 0.6

_DDL = [
    """CREATE TABLE IF NOT EXISTS ontology.competency_questions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        code VARCHAR(20) NOT NULL UNIQUE,
        question TEXT NOT NULL,
        sample_question TEXT,
        intent VARCHAR(20) NOT NULL DEFAULT 'lookup',
        subject_type VARCHAR(40),
        subject_side VARCHAR(8) NOT NULL DEFAULT 'source',
        answer_shape VARCHAR(20) NOT NULL DEFAULT 'entity',
        required_relations JSONB NOT NULL DEFAULT '[]',
        required_attributes JSONB NOT NULL DEFAULT '[]',
        min_evidence SMALLINT NOT NULL DEFAULT 1,
        priority SMALLINT NOT NULL DEFAULT 3,
        domain VARCHAR(40),
        status VARCHAR(20) NOT NULL DEFAULT 'draft',
        owner VARCHAR(60),
        notes TEXT,
        reviewed_by VARCHAR(60),
        reviewed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS ontology.cq_coverage_snapshots (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        cq_id UUID NOT NULL REFERENCES ontology.competency_questions(id) ON DELETE CASCADE,
        run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        state VARCHAR(20) NOT NULL,
        score NUMERIC(4,3) NOT NULL DEFAULT 0,
        gaps JSONB NOT NULL DEFAULT '[]',
        requirements JSONB NOT NULL DEFAULT '[]',
        plan_used JSONB NOT NULL DEFAULT '{}'
    )""",
    # 老库升级：快照补 total（该轮评估时主语类型的实体总数）
    "ALTER TABLE ontology.cq_coverage_snapshots ADD COLUMN IF NOT EXISTS total INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_cq_status ON ontology.competency_questions (status, priority)",
    ("CREATE INDEX IF NOT EXISTS idx_cq_snapshot_cq "
     "ON ontology.cq_coverage_snapshots (cq_id, run_at DESC)"),
]

_ensured = False
_ensure_lock = threading.Lock()


def ensure_schema() -> None:
    """幂等建表；单条失败不阻断（服务启动初期库可能未就绪）。"""
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        for stmt in _DDL:
            try:
                db.execute(stmt)
            except Exception:  # noqa: BLE001
                continue
        _ensured = True


# ---------------------------------------------------------------- 种子问题
# 16 条，按「一个岗位的人真的会问」来写，不是为了凑数：
# - 11 条是当前数据能部分回答的（用来量化缺口，不是用来炫耀已有）；
# - 5 条刻意写成现在答不了的，分别对应四类缺口（缺关系类型/缺属性/缺类型声明/缺实例）。
SEEDS: list[dict] = [
    {
        "code": "CQ-001", "priority": 1, "domain": "Identity",
        "question": "某个人的直接汇报对象是谁？",
        "sample_question": "某某向谁汇报？",
        "intent": "lookup", "subject_type": "Person", "subject_side": "source",
        "answer_shape": "entity", "required_relations": ["reports_to"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "组织链路的基础问题。图上现有 2 条 reports_to 边，"
                 "答不出的多数人应提示「建关系」而不是从文本里猜。",
    },
    {
        "code": "CQ-002", "priority": 1, "domain": "Identity",
        "question": "某个人的职级与岗位是什么？",
        "sample_question": "某某是什么级别、什么岗位？",
        "intent": "lookup", "subject_type": "Person", "subject_side": "source",
        "answer_shape": "value", "required_relations": [],
        "required_attributes": ["level", "job_title"], "min_evidence": 1,
        "notes": "属性类问题，走事实库不走图。判断「实体建了但属性没抽」的典型场景。",
    },
    {
        "code": "CQ-003", "priority": 1, "domain": "Identity",
        "question": "某个人在哪家公司、哪个部门？",
        "sample_question": "某某在哪个部门？",
        "intent": "lookup", "subject_type": "Person", "subject_side": "source",
        "answer_shape": "list", "required_relations": ["works_at", "member_of"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "两条关系任一成立即可回答，所以按并集算覆盖；部门链断裂会让"
                 "「按部门统计」类问题整体失效。",
    },
    {
        "code": "CQ-004", "priority": 1, "domain": "Experience",
        "question": "某个人负责或参与了哪些项目？",
        "sample_question": "某某负责哪些项目？",
        "intent": "lookup", "subject_type": "Person", "subject_side": "source",
        "answer_shape": "list",
        "required_relations": ["is_project_manager_of", "works_on"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "岗位画像的核心维度。注意 works_on 的宾语常是整句描述而非项目名，"
                 "这类事实投影不到边上。",
    },
    {
        "code": "CQ-005", "priority": 1, "domain": "Experience",
        "question": "某个项目的负责人是谁？",
        "sample_question": "数据安全项目的负责人是谁？",
        "intent": "lookup", "subject_type": "Project", "subject_side": "target",
        "answer_shape": "entity", "required_relations": ["is_project_manager_of"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "反向问题：主语是项目、关系从人指向项目。用来验证 subject_side 能翻转。",
    },
    {
        "code": "CQ-006", "priority": 2, "domain": "Experience",
        "question": "某个项目有哪些人参与？",
        "sample_question": "企业知识库 RAG 方案这个项目有哪些人参与？",
        "intent": "lookup", "subject_type": "Project", "subject_side": "target",
        "answer_shape": "list",
        "required_relations": ["works_on", "participated_in"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "项目侧的人员盘点。participated_in 的宾语多为会议而非项目，"
                 "所以真实覆盖率会比边数看起来更低。",
    },
    {
        "code": "CQ-007", "priority": 2, "domain": "Relationship",
        "question": "两个人之间通过什么关系链关联？",
        "sample_question": "某某和某某丁是怎么关联上的？",
        "intent": "path", "subject_type": "Person", "subject_side": "source",
        "answer_shape": "list",
        "required_relations": ["reports_to", "collaborates_with"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "走 graph.paths_query（≤3 跳、带环保护）。"
                 "覆盖率高说明组织关系成网，而不是一堆孤立点。",
    },
    {
        "code": "CQ-008", "priority": 2, "domain": "Identity",
        "question": "某个人在指定时点担任什么角色？",
        "sample_question": "某某在 2026 年 11 月的角色是什么？",
        "intent": "temporal", "subject_type": "Person", "subject_side": "source",
        "answer_shape": "value", "required_relations": ["has_role"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "检验双时间线是否真能回答「当时是什么」。has_role 目前只有少量边。",
    },
    {
        "code": "CQ-009", "priority": 2, "domain": "Identity",
        "question": "某个部门/组织下有多少人、都是谁？",
        "sample_question": "数据管理部下面有几个人？",
        "intent": "aggregate", "subject_type": "Organization", "subject_side": "target",
        "answer_shape": "count", "required_relations": ["works_at", "member_of"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "聚合类：先要有 Organization 实体与人员归属边，count 才有意义。",
    },
    {
        "code": "CQ-010", "priority": 2, "domain": "Cognition",
        "question": "某个人持有哪些观点或偏好？",
        "sample_question": "某某在数据准确率这件事上持什么观点？",
        "intent": "lookup", "subject_type": "Person", "subject_side": "source",
        "answer_shape": "list", "required_relations": ["believes", "prefers"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "事实库里有 49 条 believes、10 条 prefers，但宾语都是整句文本、"
                 "没有对应实体，所以图上几乎是空的——这条 CQ 专门暴露「观点未实体化」。",
    },
    {
        "code": "CQ-011", "priority": 1, "domain": "Knowledge",
        "question": "某项制度或规则约束哪些角色与流程？",
        "sample_question": "《信息保密管理制度》约束谁、管哪些流程？",
        "intent": "lookup", "subject_type": "Policy", "subject_side": "source",
        "answer_shape": "list", "required_relations": ["applies_to"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "本体目录里没有 applies_to 这个关系类型。这条 CQ 是新增关系类型的依据，"
                 "也是「CQ 驱动本体扩展」的第一个真实样例（见规则校验页）。",
    },
    {
        "code": "CQ-012", "priority": 3, "domain": "Knowledge",
        "question": "某份文档是谁写的、引用了什么？",
        "sample_question": "《信息系统安全基线规范》是谁写的？引用了哪些文档？",
        "intent": "lookup", "subject_type": "Document", "subject_side": "target",
        "answer_shape": "list", "required_relations": ["authored", "references"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "证据链回溯源头的关键问题。authored / references 目前在图上都是 0 条边。",
    },
    {
        "code": "CQ-013", "priority": 2, "domain": "Knowledge",
        "question": "某个系统的负责人是谁、谁能登录？",
        "sample_question": "ChatBI 这个系统谁在负责？",
        "intent": "lookup", "subject_type": "System", "subject_side": "target",
        "answer_shape": "list", "required_relations": ["owns", "can_login_via"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "库里有 6 个 System 实体，但已发布本体 v1.0.0 的 domains 里没有声明 "
                 "System 类型——这条 CQ 会同时报「类型未声明」和「关系无实例」两个缺口。",
    },
    {
        "code": "CQ-014", "priority": 1, "domain": "Experience",
        "question": "某个项目的计划完成时间是什么时候？",
        "sample_question": "数据安全项目什么时候交付？",
        "intent": "temporal", "subject_type": "Project", "subject_side": "source",
        "answer_shape": "value", "required_relations": [],
        "required_attributes": ["due_date", "plans_to_complete"], "min_evidence": 1,
        "notes": "完成时间散落在 responsible_for 的文本里（「完成时间2026.7.30」），"
                 "没有结构化字段。这条 CQ 用来推动「把日期从句子抽成属性」。",
    },
    {
        "code": "CQ-015", "priority": 3, "domain": "Cognition",
        "question": "某个人掌握哪些技能？",
        "sample_question": "某某会哪些技能？",
        "intent": "lookup", "subject_type": "Person", "subject_side": "source",
        "answer_shape": "list", "required_relations": ["knows"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "Skill 只有 1 个实体、knows 0 条边。用来验证「实体类型存在但实例极少」"
                 "这种缺口不会被误报成「本体缺类型」。",
    },
    {
        "code": "CQ-016", "priority": 2, "domain": "Experience",
        "question": "某个项目是否已立项且有负责人？",
        "sample_question": "L2 Agentic 精化层这个项目立项了吗、有负责人吗？",
        "intent": "boolean", "subject_type": "Project", "subject_side": "target",
        "answer_shape": "yes_no", "required_relations": ["is_project_manager_of"],
        "required_attributes": [], "min_evidence": 1,
        "notes": "存在性判定：有边且证据齐全才答「是」，否则必须答「不确定」，"
                 "不能把「没记录」当成「没有」。",
    },
]


# ---------------------------------------------------------------- 行处理
def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _normalize(row: dict) -> dict:
    out = dict(row)
    out["required_relations"] = _as_list(out.get("required_relations"))
    out["required_attributes"] = _as_list(out.get("required_attributes"))
    out["id"] = str(out["id"])
    if out.get("score") is not None:
        out["score"] = float(out["score"])
    return out


_COLS = ("id, code, question, sample_question, intent, subject_type, subject_side, "
         "answer_shape, required_relations, required_attributes, min_evidence, "
         "priority, domain, status, owner, notes, reviewed_by, reviewed_at, "
         "created_at, updated_at")


def list_cqs(status: str | None = None, domain: str | None = None,
             intent: str | None = None, keyword: str | None = None) -> dict:
    ensure_schema()
    where, params = [], {}
    if status:
        where.append("c.status = :st")
        params["st"] = status
    if domain:
        where.append("c.domain = :dm")
        params["dm"] = domain
    if intent:
        where.append("c.intent = :it")
        params["it"] = intent
    if keyword:
        where.append("(c.question ILIKE :kw OR c.code ILIKE :kw OR c.sample_question ILIKE :kw)")
        params["kw"] = f"%{keyword}%"
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = db.fetch_all(
        "SELECT c.id, c.code, c.question, c.sample_question, c.intent, c.subject_type, "
        "       c.subject_side, c.answer_shape, c.required_relations, "
        "       c.required_attributes, c.min_evidence, c.priority, c.domain, c.status, "
        "       c.owner, c.notes, c.reviewed_by, c.reviewed_at, c.created_at, c.updated_at, "
        "       s.state, s.score, s.run_at, s.total, "
        "       COALESCE(jsonb_array_length(s.gaps), 0) AS gap_count "
        "FROM ontology.competency_questions c "
        "LEFT JOIN LATERAL ("
        "  SELECT state, score, run_at, gaps, total FROM ontology.cq_coverage_snapshots x "
        "  WHERE x.cq_id = c.id ORDER BY x.run_at DESC LIMIT 1"
        ") s ON TRUE" + clause + " ORDER BY c.priority ASC, c.code ASC",
        params,
    )
    items = [_normalize(r) for r in rows]
    names = _entity_names() if any(i.get("sample_question") for i in items) else []
    for it in items:
        it["intent_label"] = INTENT_LABELS.get(it["intent"], it["intent"])
        it["status_label"] = STATUS_LABELS.get(it["status"], it["status"])
        it["sample"] = sample_check(it, names) if it.get("sample_question") else None
    return {
        "total": len(items),
        "items": items,
        "counts": {
            "draft": sum(1 for i in items if i["status"] == "draft"),
            "active": sum(1 for i in items if i["status"] == "active"),
            "retired": sum(1 for i in items if i["status"] == "retired"),
        },
    }


def get_cq(cq_id: str) -> dict | None:
    ensure_schema()
    row = db.fetch_one(
        f"SELECT {_COLS} FROM ontology.competency_questions WHERE id = CAST(:i AS uuid)",
        {"i": cq_id})
    return _normalize(row) if row else None


def _next_code() -> str:
    row = db.fetch_one(
        "SELECT max(NULLIF(regexp_replace(code, '\\D', '', 'g'), '')::int) AS n "
        "FROM ontology.competency_questions")
    n = (row or {}).get("n") or 0
    return f"CQ-{n + 1:03d}"


def _validate_payload(data: dict, creating: bool) -> dict:
    out: dict = {}
    if creating or "question" in data:
        q = (data.get("question") or "").strip()
        if len(q) < 4:
            raise ValueError("问题描述太短（至少 4 个字）")
        if len(q) > 400:
            raise ValueError("问题描述太长（上限 400 字）")
        out["question"] = q
    if "sample_question" in data:
        out["sample_question"] = (data.get("sample_question") or "").strip() or None
    if "intent" in data or creating:
        intent = (data.get("intent") or "lookup").strip()
        if intent not in INTENTS:
            raise ValueError(f"未知意图：{intent}（可选 {', '.join(INTENTS)}）")
        out["intent"] = intent
    if "answer_shape" in data or creating:
        shape = (data.get("answer_shape") or "entity").strip()
        if shape not in ANSWER_SHAPES:
            raise ValueError(f"未知答案形态：{shape}")
        out["answer_shape"] = shape
    if "subject_type" in data:
        out["subject_type"] = (data.get("subject_type") or "").strip() or None
    if "subject_side" in data:
        side = (data.get("subject_side") or "source").strip()
        if side not in ("source", "target"):
            raise ValueError("subject_side 只能是 source 或 target")
        out["subject_side"] = side
    for key in ("required_relations", "required_attributes"):
        if key in data:
            vals = data.get(key) or []
            if not isinstance(vals, list):
                raise ValueError(f"{key} 必须是数组")
            out[key] = [str(v).strip() for v in vals if str(v).strip()]
    for key, lo, hi in (("min_evidence", 1, 9), ("priority", 1, 3)):
        if key in data:
            try:
                val = int(data[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是整数") from None
            if not lo <= val <= hi:
                raise ValueError(f"{key} 取值范围 {lo}–{hi}")
            out[key] = val
    if "domain" in data:
        out["domain"] = (data.get("domain") or "").strip() or None
    if "notes" in data:
        out["notes"] = (data.get("notes") or "").strip() or None
    if "owner" in data:
        out["owner"] = (data.get("owner") or "").strip() or None
    if "status" in data:
        st = (data.get("status") or "draft").strip()
        if st not in STATUSES:
            raise ValueError(f"未知状态：{st}")
        out["status"] = st
    return out


def warnings_for(payload: dict) -> list[str]:
    """不阻断保存，但必须让人看见：这条 CQ 需要的东西本体里还没有。"""
    warns: list[str] = []
    declared = declared_types()
    st = payload.get("subject_type")
    if st and declared and st not in declared:
        warns.append(f"主语类型 {st} 未在已发布本体中声明")
    for rel in payload.get("required_relations") or []:
        if rel not in og.RELATION_TYPES:
            warns.append(f"关系类型 {rel} 未登记")
    return warns


def create_cq(data: dict, actor: str = "owner") -> dict:
    ensure_schema()
    payload = _validate_payload(data, creating=True)
    payload.setdefault("subject_type", None)
    payload.setdefault("subject_side", "source")
    payload.setdefault("min_evidence", 1)
    payload.setdefault("priority", 3)
    payload.setdefault("status", "draft")
    payload["code"] = (data.get("code") or "").strip() or _next_code()
    payload["owner"] = payload.get("owner") or actor
    if payload["status"] == "active":
        payload["reviewed_by"] = actor
        payload["reviewed_at"] = datetime.now(timezone.utc)
    cols = list(payload.keys())
    binds: dict = {}
    for i, key in enumerate(cols):
        val = payload[key]
        binds[f"v{i}"] = (json.dumps(val, ensure_ascii=False)
                          if key in ("required_relations", "required_attributes") else val)
    sql = (f"INSERT INTO ontology.competency_questions ({', '.join(cols)}) VALUES "
           f"({', '.join(':v' + str(i) for i in range(len(cols)))}) RETURNING id")
    cq_id = db.fetch_one(sql, binds)["id"]
    return {"id": str(cq_id), "code": payload["code"],
            "warnings": warnings_for(payload)}


def update_cq(cq_id: str, data: dict, actor: str = "owner") -> dict:
    ensure_schema()
    cur = get_cq(cq_id)
    if not cur:
        raise LookupError("CQ 不存在")
    payload = _validate_payload(data, creating=False)
    if not payload:
        return {"changed": 0, "warnings": []}
    merged = {**cur, **payload}
    if payload.get("status") == "active" and cur["status"] != "active":
        payload["reviewed_by"] = actor
        payload["reviewed_at"] = datetime.now(timezone.utc)
    sets, binds = [], {"i": cq_id}
    for key, val in payload.items():
        if key == "reviewed_at":
            sets.append(f"{key} = :{key}")
            binds[key] = val
        elif key in ("required_relations", "required_attributes"):
            sets.append(f"{key} = CAST(:{key} AS jsonb)")
            binds[key] = json.dumps(val, ensure_ascii=False)
        else:
            sets.append(f"{key} = :{key}")
            binds[key] = val
    sets.append("updated_at = NOW()")
    db.execute(
        f"UPDATE ontology.competency_questions SET {', '.join(sets)} "
        "WHERE id = CAST(:i AS uuid)", binds)
    return {"changed": len(payload), "warnings": warnings_for(merged)}


def seed(actor: str = "owner") -> dict:
    """写入种子 CQ。已存在的 code 只在原状态为 draft 时更新，不覆盖已审核内容。"""
    ensure_schema()
    existing = {r["code"]: r for r in db.fetch_all(
        "SELECT id, code, status FROM ontology.competency_questions")}
    inserted, refreshed, skipped = [], [], []
    for item in SEEDS:
        cur = existing.get(item["code"])
        if not cur:
            res = create_cq(item, actor=actor)
            inserted.append(res["code"])
        elif cur["status"] == "draft":
            update_cq(str(cur["id"]), item, actor=actor)
            refreshed.append(item["code"])
        else:
            skipped.append(item["code"])
    return {"inserted": inserted, "refreshed": refreshed, "skipped": skipped,
            "total": len(SEEDS)}


# ---------------------------------------------------------------- 确定性抽取（L1）
_TYPE_HINTS: dict[str, tuple[str, ...]] = {
    "Person": ("人员", "员工", "同事", "负责人", "经理", "领导", "谁", "人"),
    "Organization": ("组织", "公司", "部门", "团队", "集团", "事业部"),
    "Project": ("项目", "工程", "课题", "计划"),
    "System": ("系统", "平台", "工具", "应用"),
    "Document": ("文档", "方案", "报告", "规范", "材料", "文件"),
    "Policy": ("制度", "政策", "条例", "办法", "规定"),
    "Event": ("会议", "事件", "活动"),
    "Concept": ("概念", "主题", "观点"),
    "Skill": ("技能", "能力"),
    "Rule": ("规则", "准则", "标准"),
    "Process": ("流程", "工序"),
    "Component": ("组件", "模块"),
}

_RELATION_HINTS: dict[str, tuple[str, ...]] = {
    "reports_to": ("汇报", "上级", "直线经理"),
    "manages": ("管理", "管辖"),
    "member_of": ("隶属", "归属", "成员"),
    "belongs_to_department": ("部门", "所属部门"),
    "works_at": ("就职", "在哪家公司", "在哪家", "所在公司"),
    "has_role": ("角色", "担任"),
    "is_project_manager_of": ("项目负责人", "项目经理", "谁负责"),
    "is_owner_of": ("负责", "负责人"),
    "works_on": ("参与", "在做", "负责哪些项目"),
    "participated_in": ("参加过", "出席"),
    "depends_on": ("依赖",),
    "uses": ("使用",),
    "integrates_with": ("集成",),
    "can_login_via": ("登录", "权限"),
    "authored": ("撰写", "编写", "谁写的"),
    "references": ("引用",),
    "mentions": ("提及", "提到"),
    "believes": ("认为", "观点", "看法"),
    "prefers": ("偏好", "倾向"),
    "interested_in": ("关注",),
    "knows": ("掌握", "会哪些", "技能"),
    "collaborates_with": ("协作", "合作"),
    "mentored_by": ("指导", "带"),
    "announced": ("发布", "宣布"),
    "assigned_to": ("指派", "分配"),
    "completed": ("完成",),
    "part_of": ("属于", "组成部分"),
    "owns": ("拥有", "资产"),
}

_ATTR_HINTS: dict[str, tuple[str, ...]] = {
    "level": ("职级", "级别", "等级"),
    "job_title": ("岗位", "职位", "title"),
    "full_name": ("姓名", "全名", "叫什么"),
    "status": ("状态",),
    "email": ("邮箱", "邮件地址"),
    "hire_date": ("入职", "入职时间"),
    "department": ("部门",),
    "due_date": ("完成时间", "截止", "交付时间", "什么时候完成"),
}

_INTENT_HINTS = (
    ("aggregate", ("多少", "几个", "数量", "统计", "分别")),
    ("boolean", ("是否", "有没有", "是不是", "有没有")),
    ("path", ("怎么关联", "关系链", "通过谁", "怎么认识", "链路")),
    ("temporal", ("什么时候", "当时", "去年", "之前", "某时", "时间点", "截止")),
    ("compare", ("谁更", "对比", "比较", "哪个更")),
)


def analyze(question: str) -> dict:
    """把问句确定性对到本体：名词→类型、动词→关系、修饰→属性。

    只报「命中了什么、缺什么」，不擅自补类型；没命中就如实说没命中。
    """
    ensure_schema()
    text = (question or "").strip()
    declared = declared_types()
    matched_types, matched_rels, matched_attrs = [], [], []
    type_pos: dict[str, int] = {}
    for t, words in _TYPE_HINTS.items():
        hits = [text.index(w) for w in words if w in text]
        if hits:
            matched_types.append(t)
            type_pos[t] = min(hits)
    # 主语是「最先出现的那个名词」：「ChatBI 系统的负责人是谁」里主语是系统而不是人
    matched_types.sort(key=lambda t: (type_pos[t], -len(t)))
    for rel, words in _RELATION_HINTS.items():
        if any(w in text for w in words):
            matched_rels.append(rel)
    for attr, words in _ATTR_HINTS.items():
        if any(w in text for w in words):
            matched_attrs.append(attr)
    intent = "lookup"
    for name, words in _INTENT_HINTS:
        if any(w in text for w in words):
            intent = name
            break
    # 关系优先，属性次之：一句话里两者都命中时，关系才是主语的需求
    rels = matched_rels[:4]
    attrs = [] if rels else matched_attrs[:3]
    gaps = []
    if not matched_types:
        gaps.append({"kind": "type_unknown",
                     "message": "没识别出主语类型，需要人工指定"})
    undeclared = [t for t in matched_types if declared and t not in declared]
    if undeclared:
        gaps.append({"kind": "type_undeclared",
                     "message": "类型未在已发布本体声明：" + "、".join(undeclared)})
    unregistered = [r for r in rels if r not in og.RELATION_TYPES]
    if unregistered:
        gaps.append({"kind": "relation_unregistered",
                     "message": "关系类型未登记：" + "、".join(unregistered)})
    subject_type = matched_types[0] if matched_types else None
    subject_side = "source"
    # 主语是企业资产（项目/制度/系统/文档）而关系从人指向它时，主语落在目标端
    if subject_type in ("Project", "Policy", "System", "Document", "Organization"):
        if any(r in og.RELATION_TYPES
               and subject_type in og.RELATION_TYPES[r]["target"]
               and "Person" in og.RELATION_TYPES[r]["source"] for r in rels):
            subject_side = "target"
    return {
        "question": text,
        "intent": intent,
        "intent_label": INTENT_LABELS[intent],
        "matched_types": matched_types,
        "matched_relations": matched_rels,
        "matched_attributes": matched_attrs,
        "suggested": {
            "subject_type": subject_type,
            "subject_side": subject_side,
            "intent": intent,
            "required_relations": rels,
            "required_attributes": attrs,
            "answer_shape": ("count" if intent == "aggregate"
                             else "yes_no" if intent == "boolean" else "list"),
        },
        "gaps": gaps,
        "declared_types": sorted(declared),
    }


# ---------------------------------------------------------------- 覆盖率执行器
def declared_types() -> set[str]:
    row = db.fetch_one(
        "SELECT schema_content FROM ontology.ontology_versions "
        "WHERE status = 'published' ORDER BY created_at DESC LIMIT 1")
    if not row or not row.get("schema_content"):
        return set()
    content = row["schema_content"]
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return set()
    out: set[str] = set()
    for spec in (content.get("domains") or {}).values():
        if isinstance(spec, dict):
            out.update(spec.get("entities") or [])
    return out


def _entity_ids(etype: str | None) -> set[str]:
    if etype:
        rows = db.fetch_all(
            "SELECT id FROM ontology.entities WHERE status='active' AND entity_type = :t",
            {"t": etype})
    else:
        rows = db.fetch_all("SELECT id FROM ontology.entities WHERE status='active'")
    return {str(r["id"]) for r in rows}


def _rel_subject_ids(etype: str | None, rtype: str, side: str) -> set[str]:
    col = "r.target_entity_id" if side == "target" else "r.source_entity_id"
    sql = (f"SELECT DISTINCT {col} AS id FROM ontology.relations r "
           "JOIN ontology.entities e ON e.id = " + col + " "
           "WHERE r.status='active' AND e.status='active' AND r.relation_type = :rt")
    params = {"rt": rtype}
    if etype:
        sql += " AND e.entity_type = :t"
        params["t"] = etype
    return {str(r["id"]) for r in db.fetch_all(sql, params)}


def _rel_evidence(etype: str | None, rels: list[str], side: str) -> tuple[int, int]:
    if not rels:
        return 0, 0
    col = "r.target_entity_id" if side == "target" else "r.source_entity_id"
    ph = ", ".join(f":r{i}" for i in range(len(rels)))
    sql = (f"SELECT count(*) AS n, count(r.evidence_claim_id) AS ev "
           "FROM ontology.relations r JOIN ontology.entities e ON e.id = " + col + " "
           f"WHERE r.status='active' AND e.status='active' AND r.relation_type IN ({ph})")
    params: dict = {f"r{i}": v for i, v in enumerate(rels)}
    if etype:
        sql += " AND e.entity_type = :t"
        params["t"] = etype
    row = db.fetch_one(sql, params) or {}
    return int(row.get("n") or 0), int(row.get("ev") or 0)


def _attr_subject_ids(etype: str | None, attrs: list[str]) -> set[str]:
    if not attrs:
        return set()
    ph = ", ".join(f":p{i}" for i in range(len(attrs)))
    where = [f"f.predicate IN ({ph})"]
    params: dict = {f"p{i}": v for i, v in enumerate(attrs)}
    if "department" in attrs:
        where.append("f.predicate LIKE 'department-%'")
    sql = ("SELECT DISTINCT f.subject_id AS id FROM knowledge.facts f "
           "JOIN ontology.entities e ON e.id = f.subject_id "
           "WHERE f.status='active' AND e.status='active' AND ("
           + " OR ".join(where) + ")")
    if etype:
        sql += " AND e.entity_type = :t"
        params["t"] = etype
    return {str(r["id"]) for r in db.fetch_all(sql, params)}


def _candidate_facts(rtype: str) -> int:
    """谓词能映射到该关系、但还没成为边的事实有多少条——提示「可一键建边」。"""
    preds = [p for p, r in proj.PREDICATE_ALIASES.items() if r == rtype] + [rtype]
    ph = ", ".join(f":c{i}" for i in range(len(preds)))
    row = db.fetch_one(
        f"SELECT count(*) AS n FROM knowledge.facts "
        f"WHERE status='active' AND predicate IN ({ph})",
        {f"c{i}": v for i, v in enumerate(preds)})
    return int((row or {}).get("n") or 0)


def _reachable_ids(etype: str | None) -> set[str]:
    sql = ("SELECT DISTINCT e.id FROM ontology.entities e "
           "JOIN ontology.relations r "
           "  ON r.source_entity_id = e.id OR r.target_entity_id = e.id "
           "WHERE r.status='active' AND e.status='active'")
    params: dict = {}
    if etype:
        sql += " AND e.entity_type = :t"
        params["t"] = etype
    return {str(r["id"]) for r in db.fetch_all(sql, params)}


def _timed_ids(etype: str | None) -> set[str]:
    sql = ("SELECT DISTINCT f.subject_id AS id FROM knowledge.facts f "
           "JOIN ontology.entities e ON e.id = f.subject_id "
           "WHERE f.status='active' AND e.status='active' AND f.valid_from IS NOT NULL")
    if etype:
        sql += " AND e.entity_type = :t"
    return {str(r["id"]) for r in db.fetch_all(sql, {"t": etype} if etype else {})}


def _ratio(covered: int, total: int) -> float:
    return round(covered / total, 3) if total else 0.0


def _entity_names() -> list[tuple[str, str, str]]:
    rows = db.fetch_all(
        "SELECT id, canonical_name, entity_type FROM ontology.entities "
        "WHERE status='active' AND length(canonical_name) >= 2")
    return [(r["canonical_name"], str(r["id"]), r["entity_type"]) for r in rows]


def sample_check(cq: dict, names: list[tuple[str, str, str]] | None = None) -> dict | None:
    """用样例问句里出现的真实实体去实际取一次值。

    类型级覆盖率低不等于「这条问题完全没用」——某个具体人可能是答得出的。
    审核 CQ 时最需要的正是这个区别：问题是好的，只是数据只覆盖了个别实例。
    """
    sample = (cq.get("sample_question") or "").strip()
    if not sample:
        return None
    names = names if names is not None else _entity_names()
    hits = [n for n in names if n[0] in sample]
    if not hits:
        return {"found": False, "entity": None, "values": [],
                "note": "样例问句里没有库里已有的实体名"}
    hits.sort(key=lambda n: (-len(n[0]), n[0]))
    name, eid, etype = hits[0]
    side = cq.get("subject_side") or "source"
    values: list[dict] = []
    for rel in cq.get("required_relations") or []:
        if rel not in og.RELATION_TYPES:
            continue
        if side == "target":
            rows = db.fetch_all(
                "SELECT e.canonical_name AS name, e.entity_type AS type, "
                "       r.evidence_claim_id, r.relation_type "
                "FROM ontology.relations r JOIN ontology.entities e "
                "  ON e.id = r.source_entity_id "
                "WHERE r.status='active' AND r.target_entity_id = CAST(:i AS uuid) "
                "  AND r.relation_type = :rt LIMIT 8",
                {"i": eid, "rt": rel})
        else:
            rows = db.fetch_all(
                "SELECT e.canonical_name AS name, e.entity_type AS type, "
                "       r.evidence_claim_id, r.relation_type "
                "FROM ontology.relations r JOIN ontology.entities e "
                "  ON e.id = r.target_entity_id "
                "WHERE r.status='active' AND r.source_entity_id = CAST(:i AS uuid) "
                "  AND r.relation_type = :rt LIMIT 8",
                {"i": eid, "rt": rel})
        for r in rows:
            values.append({"via": rel, "value": r["name"],
                           "value_type": r["type"],
                           "evidence": bool(r["evidence_claim_id"])})
    for attr in cq.get("required_attributes") or []:
        rows = db.fetch_all(
            "SELECT predicate, object_value FROM knowledge.facts "
            "WHERE status='active' AND subject_id = CAST(:i AS uuid) "
            "AND (predicate = :p OR (:p = 'department' AND predicate LIKE 'department-%')) "
            "ORDER BY COALESCE(valid_from, recorded_at) DESC LIMIT 5",
            {"i": eid, "p": attr})
        for r in rows:
            val = r["object_value"]
            if isinstance(val, dict):
                val = val.get("value") or val.get("name") or json.dumps(val, ensure_ascii=False)
            values.append({"via": r["predicate"], "value": str(val)[:80],
                           "value_type": None, "evidence": True})
    return {
        "found": True,
        "entity": name,
        "entity_type": etype,
        "answerable": bool(values),
        "values": values[:8],
        "note": (f"样例可用：命中 {len(values)} 个取值" if values
                 else "样例不可用：实体在库，但需要的取值一条都没有"),
    }


def evaluate(cq: dict) -> dict:
    """单条 CQ 的只读评估：能答多少、缺什么、下一步点哪里。"""
    etype = cq.get("subject_type")
    side = cq.get("subject_side") or "source"
    rels = cq.get("required_relations") or []
    attrs = cq.get("required_attributes") or []
    intent = cq.get("intent") or "lookup"
    declared = declared_types()

    total = len(_entity_ids(etype)) if etype else len(_entity_ids(None))
    gaps: list[dict] = []
    requirements: list[dict] = []

    if etype and declared and etype not in declared:
        gaps.append({
            "kind": "type_undeclared", "severity": "error",
            "message": f"类型 {etype} 有实例，但已发布本体没有声明它",
            "action": {"kind": "extend_ontology", "label": "补进本体",
                       "hint": "在模式视图新增该类型，或走本体提案流程",
                       "nav": "rules"},
        })
    if total == 0:
        gaps.append({
            "kind": "entity_missing", "severity": "error",
            "message": f"库中没有 {etype or '任何'} 实体，问题无从谈起",
            "action": {"kind": "create_entity", "label": "建实体",
                       "hint": "先有实体，属性和关系才有挂载点", "nav": "edit"},
        })
        return {"state": "unanswerable", "score": 0.0, "total": 0,
                "requirements": requirements, "gaps": gaps}

    group_ratios: list[float] = []
    union_rel_ids: set[str] = set()
    for rel in rels:
        if rel not in og.RELATION_TYPES:
            gaps.append({
                "kind": "relation_unregistered", "severity": "error",
                "message": f"关系类型 {rel} 未登记，无法建边也无法校验",
                "action": {"kind": "extend_ontology", "label": "新增关系类型",
                           "hint": "必须同时声明源/目标类型与基数，否则规则校验会报错",
                           "nav": "rules"},
            })
            requirements.append({"kind": "relation", "ref": rel, "covered": 0,
                                 "total": total, "ratio": 0.0, "registered": False})
            continue
        ids = _rel_subject_ids(etype, rel, side)
        union_rel_ids |= ids
        requirements.append({"kind": "relation", "ref": rel, "covered": len(ids),
                             "total": total, "ratio": _ratio(len(ids), total),
                             "registered": True})
        if not ids:
            cands = _candidate_facts(rel)
            gaps.append({
                "kind": "relation_empty", "severity": "warn",
                "message": (f"没有一条 {og.RELATION_TYPES[rel]['label']} 关系"
                            + (f"；事实库有 {cands} 条谓词可映射，尚未成边" if cands
                               else "；事实库里也没有可用记录，需要补数据来源")),
                "action": {"kind": "create_relation", "label": "建关系",
                           "hint": ("审核对应事实即可自动建边" if cands
                                    else "先在「编辑与测试」里手工建一条，或补来源文档"),
                           "nav": "edit", "candidates": cands},
            })
    if rels:
        group_ratios.append(_ratio(len(union_rel_ids), total))

    attr_ids: set[str] = set()
    for attr in attrs:
        ids = _attr_subject_ids(etype, [attr])
        attr_ids |= ids
        requirements.append({"kind": "attribute", "ref": attr, "covered": len(ids),
                             "total": total, "ratio": _ratio(len(ids), total),
                             "registered": True})
        if not ids:
            gaps.append({
                "kind": "attribute_empty", "severity": "warn",
                "message": f"没有任何 {attr} 属性值（该属性可能还没进抽取模板）",
                "action": {"kind": "add_attribute", "label": "补属性",
                           "hint": "在结构化抽取里加该字段，或从现有文本中抽取",
                           "nav": "edit"},
            })
    if attrs:
        group_ratios.append(_ratio(len(attr_ids), total))

    if intent == "path" and not group_ratios:
        ids = _reachable_ids(etype)
        group_ratios.append(_ratio(len(ids), total))
        requirements.append({"kind": "reachability", "ref": "degree>=1",
                             "covered": len(ids), "total": total,
                             "ratio": _ratio(len(ids), total), "registered": True})
    if intent == "temporal" and not group_ratios:
        ids = _timed_ids(etype)
        group_ratios.append(_ratio(len(ids), total))
        requirements.append({"kind": "temporal", "ref": "valid_from",
                             "covered": len(ids), "total": total,
                             "ratio": _ratio(len(ids), total), "registered": True})

    edges, with_ev = _rel_evidence(etype, [r for r in rels if r in og.RELATION_TYPES], side)
    ev_ratio = _ratio(with_ev, edges) if edges else 1.0
    if edges and with_ev < edges:
        gaps.append({
            "kind": "evidence_missing", "severity": "warn",
            "message": f"{edges - with_ev}/{edges} 条关系没有证据链，答案不可溯源",
            "action": {"kind": "add_evidence", "label": "补证据",
                       "hint": "按来源文档回填 claim，或把这些边降级为待核",
                       "nav": "fact"},
        })

    base = sum(group_ratios) / len(group_ratios) if group_ratios else 0.0
    score = round(base * (0.7 + 0.3 * ev_ratio), 3) if group_ratios else 0.0
    if group_ratios and base < 0.3 and base > 0:
        gaps.append({
            "kind": "coverage_low", "severity": "info",
            "message": f"仅 {round(base * 100)}% 的 {etype or '实体'}能回答这条问题",
            "action": {"kind": "bulk_extract", "label": "批量补数据",
                       "hint": "优先补高频主语，再跑一次覆盖率看变化", "nav": "fact"},
        })

    if score >= ANSWER_THRESHOLD:
        state = "answered"
    elif score > 0:
        state = "partial"
    else:
        state = "unanswerable"

    return {"state": state, "score": score, "total": total,
            "requirements": requirements, "gaps": gaps,
            "plan": {"intent": intent, "subject_type": etype, "subject_side": side,
                     "relations": rels, "attributes": attrs,
                     "answer_shape": cq.get("answer_shape"),
                     "executor": ("facts" if attrs and not rels else
                                  "graph+facts" if rels and attrs else
                                  "graph" if rels else "facts")}}


def coverage(cq_ids: list[str] | None = None, persist: bool = True,
             only_active: bool = False) -> dict:
    """跑覆盖率：逐条评估 + 落快照（dry_run 时不写）。"""
    ensure_schema()
    sql = f"SELECT {_COLS} FROM ontology.competency_questions"
    where, params = [], {}
    if cq_ids:
        ph = ", ".join(f":i{i}" for i in range(len(cq_ids)))
        where.append(f"id IN ({ph})")
        params.update({f"i{i}": v for i, v in enumerate(cq_ids)})
    if only_active:
        where.append("status = 'active'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY priority ASC, code ASC"
    rows = [_normalize(r) for r in db.fetch_all(sql, params)]

    run_at = datetime.now(timezone.utc)
    names = _entity_names() if any(c.get("sample_question") for c in rows) else []
    items = []
    for cq in rows:
        res = evaluate(cq)
        if persist:
            db.execute(
                "INSERT INTO ontology.cq_coverage_snapshots "
                "(cq_id, run_at, state, score, gaps, requirements, plan_used, total) "
                "VALUES (CAST(:c AS uuid), :at, :st, :sc, CAST(:g AS jsonb), "
                "CAST(:r AS jsonb), CAST(:p AS jsonb), :total)",
                {"c": cq["id"], "at": run_at, "st": res["state"], "sc": res["score"],
                 "g": json.dumps(res["gaps"], ensure_ascii=False),
                 "r": json.dumps(res["requirements"], ensure_ascii=False),
                 "p": json.dumps(res.get("plan") or {}, ensure_ascii=False),
                 "total": res["total"]})
        gap_kinds: dict[str, int] = {}
        for g in res["gaps"]:
            gap_kinds[g["kind"]] = gap_kinds.get(g["kind"], 0) + 1
        items.append({
            "id": cq["id"], "code": cq["code"], "question": cq["question"],
            "intent": cq["intent"], "intent_label": INTENT_LABELS.get(cq["intent"], ""),
            "status": cq["status"], "priority": cq["priority"], "domain": cq["domain"],
            "state": res["state"], "score": res["score"], "total": res["total"],
            "gaps": res["gaps"], "requirements": res["requirements"],
            "plan": res.get("plan"),
            "gap_kinds": gap_kinds,
            "sample": sample_check(cq, names),
        })

    states = {"answered": 0, "partial": 0, "unanswerable": 0}
    for it in items:
        states[it["state"]] = states.get(it["state"], 0) + 1
    all_kinds: dict[str, int] = {}
    for it in items:
        for k, v in it["gap_kinds"].items():
            all_kinds[k] = all_kinds.get(k, 0) + v
    avg = round(sum(i["score"] for i in items) / len(items), 3) if items else 0.0
    return {
        "run_at": run_at.isoformat(),
        "persisted": persist,
        "total": len(items),
        "states": states,
        "avg_score": avg,
        "threshold": ANSWER_THRESHOLD,
        "gap_kinds": all_kinds,
        "items": items,
    }
