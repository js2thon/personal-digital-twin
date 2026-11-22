"""CQ V2.0 门面服务：把四层建模、质量准入、Planner、执行/评估、决策与演进闭环串起来。

对外只暴露 CQEngine；API 层、CLI 与测试都通过它调用。
"""
from __future__ import annotations

import uuid

from . import capability as cap
from . import decision as dec
from . import engine as eng
from . import evolution as evo
from . import mapping as mp
from . import models as m
from . import planner as pl
from . import quality as q
from . import registry as reg
from . import store as st

# 16 条种子 CQ 的六维示范初评（与 V2.0 方案 §7 一致，需业务方复核）
SEED_SCORES = {
    "CQ-001": (4, 4, 3, 4, 4, 4), "CQ-002": (4, 3, 3, 4, 3, 4),
    "CQ-003": (3, 3, 2, 4, 3, 4), "CQ-004": (3, 3, 2, 3, 3, 3),
    "CQ-005": (4, 4, 4, 4, 3, 4), "CQ-006": (3, 4, 3, 3, 2, 4),
    "CQ-007": (3, 2, 1, 3, 3, 3), "CQ-008": (4, 4, 3, 4, 3, 4),
    "CQ-009": (3, 3, 2, 4, 3, 4), "CQ-010": (3, 2, 1, 2, 2, 2),
    "CQ-011": (4, 3, 2, 3, 3, 3), "CQ-012": (3, 3, 3, 3, 2, 3),
    "CQ-013": (3, 2, 2, 3, 2, 3), "CQ-014": (4, 4, 3, 4, 3, 4),
    "CQ-015": (3, 3, 2, 3, 2, 3), "CQ-016": (3, 2, 2, 3, 3, 3),
}

CAPABILITY_SEEDS = [
    ("CAP-ORG", "组织人事查询", "组织人事", "normal"),
    ("CAP-PROJ", "项目与履历管理", "项目管理", "high"),
    ("CAP-REL", "关系网络分析", "关系网络", "normal"),
    ("CAP-COG", "认知与偏好理解", "认知偏好", "normal"),
    ("CAP-KNOW", "知识与制度检索", "知识制度", "normal"),
]
DOMAIN_TO_CAP = {
    "Identity": "CAP-ORG", "Experience": "CAP-PROJ", "Relationship": "CAP-REL",
    "Cognition": "CAP-COG", "Knowledge": "CAP-KNOW",
}
DOMAIN_LABEL = {
    "Identity": "组织人事", "Experience": "项目管理", "Relationship": "关系网络",
    "Cognition": "认知偏好", "Knowledge": "知识制度",
}
SOURCES_BY_DOMAIN = {
    "Identity": ["HR系统"], "Relationship": ["HR系统"], "Cognition": ["HR系统"],
    "Experience": ["项目风险台账"], "Knowledge": ["采购系统"],
}

FALLBACK_SEEDS = [
    ("CQ-001", "某人的直接汇报对象是谁？", "lookup", "Person", "source", "entity", ["reports_to"], [], "Identity"),
    ("CQ-002", "某人的职级与岗位是什么？", "lookup", "Person", "source", "value", [], ["level", "job_title"], "Identity"),
    ("CQ-003", "某人在哪家公司、哪个部门？", "lookup", "Person", "source", "list", ["works_at", "member_of"], [], "Identity"),
    ("CQ-004", "某人负责或参与了哪些项目？", "lookup", "Person", "source", "list", ["is_project_manager_of", "works_on"], [], "Experience"),
    ("CQ-005", "某个项目的负责人是谁？", "lookup", "Project", "target", "entity", ["is_project_manager_of"], [], "Experience"),
    ("CQ-006", "某个项目有哪些人参与？", "lookup", "Project", "target", "list", ["works_on", "participated_in"], [], "Experience"),
    ("CQ-007", "两个人之间通过什么关系链关联？", "path", "Person", "source", "list", ["reports_to", "collaborates_with"], [], "Relationship"),
    ("CQ-008", "某人在指定时点担任什么角色？", "temporal", "Person", "source", "value", ["has_role"], [], "Identity"),
    ("CQ-009", "某部门/组织下有多少人、都是谁？", "aggregate", "OrganizationUnit", "target", "count", ["works_at", "member_of"], [], "Identity"),
    ("CQ-010", "某人持有哪些观点或偏好？", "lookup", "Person", "source", "list", ["believes", "prefers"], [], "Cognition"),
    ("CQ-011", "某项制度或规则约束哪些角色与流程？", "lookup", "Policy", "source", "list", ["applies_to"], [], "Knowledge"),
    ("CQ-012", "某份文档是谁写的、引用了什么？", "lookup", "Document", "target", "list", ["authored", "references"], [], "Knowledge"),
    ("CQ-013", "某个系统的负责人是谁、谁能登录？", "lookup", "System", "target", "list", ["owns", "can_login_via"], [], "Knowledge"),
    ("CQ-014", "某个项目的计划完成时间是什么时候？", "temporal", "Project", "source", "value", [], ["due_date"], "Experience"),
    ("CQ-015", "某人掌握哪些技能？", "lookup", "Person", "source", "list", ["knows"], [], "Cognition"),
    ("CQ-016", "某个项目是否已立项且有负责人？", "boolean", "Project", "target", "yes_no", ["is_project_manager_of"], [], "Experience"),
]


def _template_of(question: str) -> str:
    """把具体问句参数化：{person} / {project} / {as_of} 等占位符。"""
    pairs = (
        ("某个人", "{person}"), ("某人", "{person}"),
        ("某个项目", "{project}"), ("某部门/组织", "{organization}"),
        ("某项制度或规则", "{policy}"), ("某项制度", "{policy}"),
        ("某份文档", "{document}"), ("某个系统", "{system}"),
        ("两个人", "{person_a} 和 {person_b}"),
    )
    out = question
    for old, new in pairs:
        if old in out:
            out = out.replace(old, new, 1)
            break
    return out


def demo_snapshot() -> dict:
    """可复现的演示数据快照：覆盖 answered / conflict / restricted / 六类缺口。"""
    return {
        "data_snapshot_id": "DS-DEMO-001",
        "entities": {
            "Person": ["e1", "e2", "e3"], "Project": ["p1", "p2"],
            "OrganizationUnit": ["o1"], "System": ["s1"], "Policy": ["pol1"],
            "Document": ["d1"], "Skill": ["sk1"],
        },
        "facts": [
            {"subject_id": "e1", "predicate": "reports_to", "object": "某某乙",
             "evidence_refs": ["ev1"], "valid_from": "2025-01-01", "recorded_at": "2025-01-02"},
            {"subject_id": "e2", "predicate": "reports_to", "object": "李四",
             "evidence_refs": ["ev2"], "valid_from": "2024-01-01", "recorded_at": "2024-01-02"},
            {"subject_id": "e1", "predicate": "job_title", "object": "数据工程师",
             "evidence_refs": ["ev1"]},
            {"subject_id": "e1", "predicate": "level", "object": "P7", "evidence_refs": ["ev1"]},
            {"subject_id": "e3", "predicate": "believes", "object": "口径待核",
             "evidence_refs": [], "conflict": True},
            {"subject_id": "p1", "predicate": "due_date", "object": "2026-07-30",
             "evidence_refs": ["ev3"], "valid_from": "2026-01-01"},
            {"subject_id": "e1", "predicate": "has_role", "object": "角色口径待裁决",
             "evidence_refs": ["ev5"], "conflict": True},
            {"subject_id": "p1", "predicate": "is_project_manager_of",
             "object": "某某", "evidence_refs": ["ev4"], "restricted": True},
        ],
        "attributes": {"e1": {"level": "P7", "job_title": "数据工程师", "_evidence": ["ev1"]},
                       "p1": {"due_date": "2026-07-30", "_evidence": ["ev3"]}},
        "relations": [
            {"type": "reports_to", "source": "e1", "target": "e2", "evidence": ["ev1"]},
            {"type": "works_at", "source": "e1", "target": "o1", "evidence": ["ev1"]},
            {"type": "collaborates_with", "source": "e2", "target": "e3", "evidence": ["ev2"]},
            {"type": "is_project_manager_of", "source": "e1", "target": "p1",
             "evidence": ["ev4"]},
        ],
        "documents": [
            {"id": "d1", "text": "信息保密管理制度第三条：……", "source": "HR系统",
             "evidence_refs": ["ev1"]},
            {"id": "d2", "text": "采购合同约定：……", "source": "采购系统",
             "evidence_refs": [], "restricted": True},
        ],
        "evidence_catalog": {
            "ev1": {"authority": 0.95, "expired": False},
            "ev2": {"authority": 0.90, "expired": True},
            "ev3": {"authority": 0.40, "expired": False},
            "ev4": {"authority": 0.92, "expired": False},
            "ev5": {"authority": 0.88, "expired": False},
        },
        "source_status": {"HR系统": "ok", "项目风险台账": "not_connected",
                          "采购系统": "unauthorized", "财务系统": "sync_failed"},
    }


STATE_REGISTRY = "schema_registry"
STATE_AUDIT = "audit_log"
STATE_BASELINE = "baseline"


class CQEngine:
    def __init__(self, store=None):
        self.store = store or st.default_store()
        self.registry = reg.SchemaRegistry(reg.default_schema())
        self.baseline: dict = {}
        self.audit_log: list[dict] = []
        self.restored_at: str | None = None
        self.persisted_at: str | None = None
        self.ready_report: dict | None = None

    # ------------------------------------------------------------ 持久化生命周期
    def persist_state(self) -> dict:
        """把非表结构的运行态写进 KV：Registry、审计、基线。

        表数据是写穿透的（每次 insert/update 即落库），这里补的是那几个
        「重启后就没了」的进程内对象。
        """
        if not getattr(self.store, "durable", False):
            return {"durable": False, "skipped": True,
                    "reason": self.store.degraded_reason or "memory store 不持久化"}
        self.store.write_state(STATE_REGISTRY, self.registry.to_state())
        self.store.write_state(STATE_AUDIT, {"items": self.audit_log[-500:]})
        self.store.write_state(STATE_BASELINE, dict(self.baseline))
        self.persisted_at = m.now()
        return {"durable": True, "skipped": False,
                "registry_version": self.registry.current_version(),
                "audit_entries": len(self.audit_log),
                "baseline_keys": sorted(self.baseline),
                "at": self.persisted_at}

    def restore(self, force: bool = False) -> dict:
        """从库里的权威副本水合内存态。已有数据且 force=False 时不动。"""
        if not getattr(self.store, "durable", False):
            return {"durable": False, "restored": False,
                    "reason": self.store.degraded_reason or "memory store 无外部副本"}
        snap = self.store.load_snapshot()
        if not (snap.get("tables") or snap.get("state")):
            return {"durable": True, "restored": False, "reason": "库内无快照"}
        if self.store.all("cqs") and not force:
            return {"durable": True, "restored": False, "reason": "内存态已有数据"}

        counts = self.store.restore(snap)
        state = snap.get("state") or {}
        reg_ok = self.registry.load_state(state.get(STATE_REGISTRY))
        self.audit_log = list((state.get(STATE_AUDIT) or {}).get("items") or [])
        self.baseline = dict(state.get(STATE_BASELINE) or {})
        self.restored_at = m.now()
        return {"durable": True, "restored": True, "at": self.restored_at,
                "tables": counts.get("tables", {}),
                "total_objects": counts.get("total_objects", 0),
                "cqs": len(self.store.all("cqs")),
                "registry_version": self.registry.current_version(),
                "registry_restored": reg_ok,
                "audit_entries": len(self.audit_log),
                "baseline_keys": sorted(self.baseline)}

    def ensure_ready(self, auto_admit_actor: str | None = None) -> dict:
        """启动入口：建表 → 水合 → 空库才播种 → 对齐双轨 → 回写状态。可反复调用。"""
        ddl = self.store.ensure_schema()
        restored = self.restore()
        seeded = None
        if not self.store.all("cqs"):
            seeded = self.bootstrap(auto_admit_actor=auto_admit_actor)
        # P1-2：Registry 版本落 schema_registry（含 LinkML）并同步到 ontology_versions，
        # 否则「JSONB 有版本、关系表没有」的双轨分叉会在重启后继续存在。
        schemas = self.persist_schema_versions()
        runtime = self.sync_runtime_versions()
        # 关系轨落后（旧数据先于双写上线）时启动即补齐；已一致则跳过，不做无谓写。
        tracks = self.store.tracks()
        repaired = (self.store.sync_tracks()
                    if tracks.get("comparable") and not tracks.get("in_sync")
                    else {"skipped": True, "in_sync": tracks.get("in_sync")})
        # 派生表（能力关联 / 需求项）必须指向真实 CQ，否则关系轨写不进去。
        # 这里只报告不自动删改：重建是显式动作（/derived/rebuild，默认 dry run）。
        derived = self.derived_status()
        persisted = self.persist_state()
        self.ready_report = {
            "store_mode": self.store.mode, "durable": self.store.durable,
            "degraded_reason": self.store.degraded_reason,
            "ddl": ddl, "restored": restored, "seeded": seeded,
            "schemas": schemas, "runtime_versions": runtime,
            "tracks": {"in_sync": tracks.get("in_sync"), "repaired": repaired},
            "derived": derived,
            "persisted": persisted, "at": m.now(),
            "cqs": len(self.store.all("cqs")),
            "registry_version": self.registry.current_version(),
        }
        return self.ready_report

    def flush(self) -> dict:
        """强制把内存态全量回写库（含表数据），用于漂移修复。"""
        state = self.persist_state()
        if not state.get("durable"):
            return {**state, "tables_written": 0}
        if hasattr(self.store, "flush_all"):
            written = self.store.flush_all()
            return {**state, "tables_written": written.get("written", 0),
                    "tables": written.get("tables", 0)}
        return {**state, "tables_written": 0}

    def persistence_status(self) -> dict:
        st = self.store.status()
        counts = st.get("tables") or {}
        return {
            "store_mode": st.get("mode"), "durable": st.get("durable"),
            "degraded_reason": st.get("degraded_reason") or None,
            "restored_at": self.restored_at, "persisted_at": self.persisted_at,
            "cqs": counts.get("cqs", len(self.store.all("cqs"))),
            "capabilities": counts.get("capabilities", 0),
            "requirements": counts.get("cq_requirements", 0),
            "objects_in_memory": st.get("total_objects", 0),
            "objects_in_db": st.get("db_objects"),
            "registry_version": self.registry.current_version(),
            "registry_versions": len(self.registry.versions),
            "migrations": len(self.registry.migrations),
            "pins": len(self.registry.pins),
            "audit_entries": len(self.audit_log),
            "baseline_keys": sorted(self.baseline),
            "state_keys": st.get("state_keys") or [],
        }

    def verify_persistence(self) -> dict:
        """内存读模型 vs 库内权威副本逐表对账；不可比时如实说明原因。"""
        if not getattr(self.store, "durable", False):
            return {"comparable": False, "in_sync": False,
                    "reason": self.store.degraded_reason or "memory store 无外部副本"}
        drift = self.store.drift()
        status = self.persistence_status()
        return {**drift, "store_mode": status["store_mode"],
                "objects_in_memory": status["objects_in_memory"],
                "objects_in_db": status["objects_in_db"],
                "registry_version": status["registry_version"],
                "audit_entries": status["audit_entries"]}

    def _audit(self, step: str, detail: dict | None = None,
               actor: str = "system") -> dict:
        """追加审计并立即持久化，避免重启丢审计链。"""
        entry = evo.audit_entry(step, detail, actor)
        self.audit_log.append(entry)
        self.persist_state()
        return entry

    # ------------------------------------------------------------ 基础
    def bootstrap(self, force: bool = False, auto_admit_actor: str | None = None) -> dict:
        ddl = self.store.ensure_schema()
        if self.store.all("cqs") and not force:
            return {"skipped": True, "reason": "已有 CQ 数据，未覆盖", "ddl": ddl}
        if force:
            self.store.clear()
            self.registry = reg.SchemaRegistry(reg.default_schema())
            self.audit_log.clear()

        caps = []
        for code, name, domain, risk in CAPABILITY_SEEDS:
            caps.append(self.store.insert("capabilities", m.new_capability(
                code=code, name=name, business_domain=domain, risk_level=risk,
                owner="业务负责人", description=f"{name}：数字员工需要具备的业务能力")))
        cap_by_code = {c["code"]: c for c in caps}

        try:
            from app.modules.ontology import cq as legacy  # 单一事实源：复用 V1.2 的 16 条
            seeds = [(s["code"], s["question"], s["intent"], s.get("subject_type", ""),
                      s.get("subject_side", "source"), s["answer_shape"],
                      s.get("required_relations", []), s.get("required_attributes", []),
                      s.get("domain", "")) for s in legacy.SEEDS]
        except Exception:  # noqa: BLE001
            seeds = FALLBACK_SEEDS

        cqs, links, reqs = [], [], []
        for code, question, intent, stype, side, shape, rels, attrs, domain in seeds:
            dims = dict(zip(m.QUALITY_DIMENSIONS, SEED_SCORES.get(code, (3, 3, 2, 3, 2, 3))))
            score, grade = q.score_dimensions(dims)
            gates = q.hard_gates(dims, score)
            cap_obj = cap_by_code.get(DOMAIN_TO_CAP.get(domain, "CAP-ORG"), caps[0])
            cq = m.new_cq(
                code=code, question=question, question_template=_template_of(question),
                intent=intent, subject_type=stype, subject_side=side,
                answer_shape=shape, required_relations=list(rels),
                required_attributes=list(attrs), domain=domain,
                business_domain=DOMAIN_LABEL.get(domain, "组织人事"),
                business_purpose="支撑该业务域的日常查询与决策",
                capability_ids=[cap_obj["id"]],
                scope_constraints={"sources": SOURCES_BY_DOMAIN.get(domain, ["HR系统"])},
                temporal_constraints={"as_of": "current"},
                acceptance_criteria={"match": "exact"},
                evidence_requirements={"min_evidence": 1, "traceable": True},
                expected_answer={}, evaluation_method="exact_match",
                min_evidence=1, risk_level=cap_obj["risk_level"],
                cq_weight=1.0, schema_version=self.registry.current_version(),
                quality_score=score, quality_dimensions=dims, hard_gates=gates,
                owner="业务负责人",
            )
            cq["grade"] = grade
            cqs.append(self.store.insert("cqs", cq))
            links.append(self.store.insert("capability_cqs", {
                "id": m.uid(), "capability_id": cap_obj["id"], "cq_id": cq["id"],
                "link_weight": 1.0}))
            for req in cap.derive_requirements(cq):
                reqs.append(self.store.insert("cq_requirements", req))

        # 参数化测试实例：标准答案来自独立的人工标注，不由 LLM 生成
        first = next((c for c in cqs if c["code"] == "CQ-001"), None)
        instances = []
        if first:
            for i, (person, answer) in enumerate(
                    [("张三", "某某乙"), ("李四", "李四"), ("王五", "某某乙")], 1):
                instances.append(self.store.insert("instances", m.new_instance(
                    cq_id=first["id"], instance_code=f"CQ-001-T0{i}",
                    question_text=m.render_template(first["question_template"],
                                                    {"person": person, "as_of": "当前"}),
                    params={"person": person, "as_of": "当前"},
                    expected_answer={"value": answer},
                    acceptance_criteria={"match": "exact", "relation": "reports_to"},
                    evidence_refs=["ev1"], source_type="human_annotation",
                    source_ref="HR 组织架构台账", annotated_by="业务负责人",
                    valid_time={"as_of": "current"}, status="active")))

        admitted = []
        if auto_admit_actor:
            for c in cqs:
                if (c["quality_score"] or 0) >= 80 and c["hard_gates"]["passed"]:
                    a = {"machine_passed": True, "hard_gates": c["hard_gates"]}
                    self.store.update("cqs", c["id"], q.admit(c, a, auto_admit_actor))
                    admitted.append(c["code"])
        self._audit("bootstrap", {"cqs": len(cqs)}, "system")
        persisted = self.persist_state()
        return {
            "skipped": False, "ddl": ddl, "capabilities": len(caps),
            "cqs": len(cqs), "links": len(links), "requirements": len(reqs),
            "test_instances": len(instances), "auto_admitted": admitted,
            "registry_version": self.registry.current_version(),
            "persisted": persisted,
        }

    def meta(self) -> dict:
        return {
            "version": "V2.0", "store_mode": self.store.mode,
            "persistence": {
                "durable": bool(getattr(self.store, "durable", False)),
                "degraded_reason": self.store.degraded_reason or None,
                "restored_at": self.restored_at, "persisted_at": self.persisted_at,
                "registry_versions": len(self.registry.versions),
                "audit_entries": len(self.audit_log),
            },
            "schema_governance": self._governance_summary(),
            "intents": [{"value": i, "label": m.INTENT_LABELS[i]} for i in m.INTENTS],
            "base_intents": list(m.BASE_INTENTS),
            "decision_intents": list(m.DECISION_INTENTS),
            "quality_dimensions": [{"key": k, "label": m.QUALITY_LABELS[k],
                                    "weight": m.QUALITY_WEIGHTS[k]}
                                   for k in m.QUALITY_DIMENSIONS],
            "coverage_dimensions": list(m.COVERAGE_DIMENSIONS),
            "answer_states": [{"value": s, "label": m.STATE_LABELS[s]}
                              for s in m.ANSWER_STATES],
            "gap_kinds": [{"value": g, "label": m.GAP_LABELS[g]} for g in m.GAP_KINDS],
            "proposal_operations": list(m.PROPOSAL_OPERATIONS),
            "plan_ops": list(m.PLAN_OPS),
            "rubric_version": m.RUBRIC_VERSION,
            "schema_version": self.registry.current_version(),
            "four_layers": ["Business Capability", "Competency Questions",
                            "Ontology Requirements", "Knowledge & Evidence"],
        }

    # ------------------------------------------------------------ 四层建模
    def capabilities(self) -> list[dict]:
        return self.store.all("capabilities")

    def create_capability(self, data: dict) -> dict:
        return self.store.insert("capabilities", m.new_capability(**data))

    def link_cq(self, capability_id: str, cq_id: str, weight: float = 1.0) -> dict:
        if not self.store.get("capabilities", capability_id):
            raise LookupError("业务能力不存在")
        if not self.store.get("cqs", cq_id):
            raise LookupError("CQ 不存在")
        return self.store.insert("capability_cqs", {
            "id": m.uid(), "capability_id": capability_id, "cq_id": cq_id,
            "link_weight": weight})

    def requirements(self, cq_id: str | None = None) -> list[dict]:
        return self.store.all("cq_requirements", cq_id=cq_id) if cq_id else \
            self.store.all("cq_requirements")

    def four_layers(self) -> dict:
        return cap.four_layer_view(self.capabilities(), self.cqs(),
                                   self.store.all("cq_requirements"),
                                   self.store.all("instances"))

    # ------------------------------------------------------------ CQ 与质量准入
    def cqs(self, **filters) -> list[dict]:
        return self.store.all("cqs", **filters)

    def get_cq(self, cq_id: str) -> dict | None:
        return self.store.get("cqs", cq_id)

    def create_cq(self, data: dict) -> dict:
        cq = m.new_cq(**data)
        cq["schema_version"] = self.registry.current_version()
        saved = self.store.insert("cqs", cq)
        for req in cap.derive_requirements(saved):
            self.store.insert("cq_requirements", req)
        self.quality_check(saved["id"])          # 新建即自动评分，无需人工触发
        return self.get_cq(saved["id"])

    def update_cq(self, cq_id: str, patch: dict) -> dict | None:
        cur = self.get_cq(cq_id)
        if not cur:
            return None
        patch = {**patch, "version": int(cur.get("version") or 1) + 1,
                 "review_status": "pending", "updated_at": m.now()}
        self.store.update("cqs", cq_id, patch)
        self.quality_check(cq_id)                # 编辑后自动重算，保证分数始终最新
        return self.get_cq(cq_id)

    def quality_check(self, cq_id: str, llm_dims: dict | None = None) -> dict:
        cq = self.get_cq(cq_id)
        if not cq:
            raise LookupError("CQ 不存在")
        instances = self.store.all("instances", cq_id=cq_id)
        out = q.four_gates(cq, existing=self.cqs(), schema=self.registry.current(),
                           instances=instances, llm_dims=llm_dims)
        self.store.update("cqs", cq_id, {
            "quality_dimensions": out["dimensions"],
            "quality_score": out["quality_score"], "grade": out["grade"],
            "hard_gates": out["hard_gates"],
            "quality_rubric_version": out["rubric_version"],
            "review_status": out["review_status"]})
        self.store.write_assessment({**out, "cq_id": cq_id,
                                     "cq_version": cq.get("version"),
                                     "weights": m.QUALITY_WEIGHTS,
                                     "admitted": out["passed"]})
        return out

    def admit_cq(self, cq_id: str, actor: str) -> dict:
        cq = self.get_cq(cq_id)
        if not cq:
            raise LookupError("CQ 不存在")
        a = {"machine_passed": (cq.get("quality_score") or 0) >= 80,
             "hard_gates": cq.get("hard_gates") or {}}
        patch = q.admit(cq, a, actor)
        self.store.update("cqs", cq_id, patch)
        self._audit("admit_cq", {"cq": cq.get("code")}, actor)
        return {"cq_id": cq_id, **patch}

    def instances(self, cq_id: str | None = None) -> list[dict]:
        return self.store.all("instances", cq_id=cq_id) if cq_id else \
            self.store.all("instances")

    def add_instance(self, data: dict) -> dict:
        cq = self.get_cq(data.get("cq_id", ""))
        if not cq:
            raise LookupError("CQ 不存在")
        inst = m.new_instance(**data)
        ok, reason = q.validate_instance(inst, cq)
        if not ok:
            raise ValueError(reason)
        saved = self.store.insert("instances", inst)
        self.quality_check(data["cq_id"])        # 实例变化影响可验证性关，自动重算
        return saved

    # ------------------------------------------------------------ Schema 治理
    def _schema_row(self, schema: dict) -> dict:
        """Registry 版本 → schema_registry 行；linkml_yaml 落库，关系轨自成建模源。

        id 由 schema_version 确定性派生（uuid5）：同一版本重复落库是「更新」而不是
        「新增一行」，否则 MemoryStore 会攒出多个同版本副本，对账就永远对不上。
        """
        from . import linkml

        ver = schema.get("schema_version") or self.registry.current_version()
        return {**schema, "schema_version": ver,
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"cq-v2:schema:{ver}")),
                "linkml_yaml": linkml.dumps(schema)}

    def persist_schema_versions(self) -> dict:
        """把所有 Registry 版本落成 schema_registry 行（幂等，按 schema_version 冲突升级）。"""
        written, failed = [], {}
        for ver in self.registry.order:
            schema = self.registry.get(ver)
            if not schema:
                continue
            try:
                self.store.insert("schema_registry", self._schema_row(schema))
                written.append(ver)
            except Exception as exc:  # noqa: BLE001  单版本失败不影响其余
                failed[ver] = str(exc)[:160]
        return {"versions": written, "written": len(written), "failed": failed}

    def sync_runtime_versions(self) -> dict:
        """把 Registry 版本同步到 `ontology.ontology_versions`（语义运行时读的版本表）。

        V1.2 的运行时按 `ontology_versions.status='published'` 解析当前版本，V2 的
        Registry 存在 `schema_registry` —— 两张表各存一份就是「版本双轨」。
        这里以 Registry 为准补齐：每个经过 Registry 发布的版本都是 published，
        运行时用 `created_at DESC` 取最新，因此当前版本 = Registry 当前版本。
        """
        db = getattr(self.store, "db", None)
        if not getattr(self.store, "durable", False) or db is None:
            return {"skipped": True, "reason": "非关系存储，无 ontology_versions 轨"}
        from . import linkml

        current = self.registry.current_version()
        rows, errors = [], {}
        for ver in self.registry.order:
            schema = self.registry.get(ver)
            if not schema:
                continue
            content = {**schema, "linkml_yaml": linkml.dumps(schema),
                       "source": "cq_v2.registry"}
            try:
                db.execute(
                    "INSERT INTO ontology.ontology_versions "
                    "(version, schema_uri, schema_content, status) "
                    "VALUES (:v, :u, CAST(:c AS jsonb), :s) "
                    "ON CONFLICT (version) DO UPDATE SET "
                    " schema_uri = EXCLUDED.schema_uri, "
                    " schema_content = EXCLUDED.schema_content, "
                    " status = EXCLUDED.status",
                    {"v": ver, "u": linkml.schema_uri(schema),
                     "c": m.json_dumps(content),
                     "s": schema.get("status") or "published"})
                rows.append(ver)
            except Exception as exc:  # noqa: BLE001
                errors[ver] = str(exc)[:160]
        return {"synced": rows, "count": len(rows), "current": current,
                "errors": errors,
                "published": self._count_published(db)}

    @staticmethod
    def _count_published(db) -> int | None:
        try:
            row = db.fetch_one(
                "SELECT count(*)::int AS n FROM ontology.ontology_versions "
                "WHERE lower(status) = 'published'")
            return int((row or {}).get("n") or 0)
        except Exception:  # noqa: BLE001
            return None

    def registry_publish(self, schema: dict, actor: str = "owner") -> dict:
        out = self.registry.publish(schema, actor)
        self.store.insert("schema_migrations", out["migration"])
        # 双轨合一：所有版本（含初始版本）落 schema_registry，并同步到 ontology_versions
        self.persist_schema_versions()
        runtime = self.sync_runtime_versions()
        out["runtime_versions"] = runtime
        self._audit("schema_publish", {**out, "runtime_versions": runtime}, actor)
        return out

    def registry_rollback(self, version: str, actor: str = "owner") -> dict:
        out = self.registry.rollback(version, actor)
        if out.get("changed"):
            rec = out.get("migration") or {}
            if rec:
                self.store.insert("schema_migrations", rec)
        runtime = self.sync_runtime_versions() if out.get("changed") else {"skipped": True}
        out["runtime_versions"] = runtime
        self._audit("schema_rollback", {"to": version, "changed": out.get("changed")},
                    actor)
        return out

    def registry_pin(self, cq_id: str, version: str | None = None) -> dict:
        out = self.registry.pin(cq_id, version or self.registry.current_version())
        self.store.insert("schema_registry",
                          self._schema_row(self.registry.get(out["schema_version"])))
        self.persist_state()
        return out

    def registry_linkml(self) -> str:
        return reg.to_linkml(self.registry.current())

    def linkml_roundtrip(self) -> dict:
        """导出→导入自证：证明 LinkML 是单一来源，而不是又一份副本。"""
        from . import linkml

        return linkml.roundtrip(self.registry.current())

    def linkml_import(self, text: str, actor: str = "owner",
                      dry_run: bool = True) -> dict:
        """把 LinkML 当建模输入：解析 → 与当前版本比 diff → 可选发布。

        dry_run=True 时只返回兼容性判断与迁移计划，不落任何数据 —— 手写 YAML
        直接进生产是双轨的另一种形态（建模源与实际 Schema 各改各的）。
        """
        from . import linkml

        schema = linkml.parse(text)
        if not schema.get("schema_version"):
            schema["schema_version"] = f"{self.registry.current_version()}-import"
        d = reg.diff(self.registry.current(), schema)
        compat = reg.compatibility(d)
        plan = reg.migration_plan(d, self.registry.current_version(),
                                  schema["schema_version"])
        out = {"parsed": True, "schema_version": schema["schema_version"],
               "classes": len(schema["entity_types"]),
               "relations": len(schema["relation_types"]),
               "attributes": len(schema["attributes"]),
               "diff": d, "compatibility": compat, "migration_plan": plan,
               "dry_run": bool(dry_run), "published": False}
        if dry_run:
            out["roundtrip"] = linkml.roundtrip(schema)
            return out
        if schema["schema_version"] in self.registry.versions:
            raise ValueError(f"Schema 版本已存在：{schema['schema_version']}")
        published = self.registry_publish(schema, actor)
        out["published"] = True
        out["publish"] = published
        return out

    # ------------------------------------------------------------ 派生表一致性
    def _expected_derived(self) -> tuple[list[dict], list[dict]]:
        """按当前 CQ 重算派生行：能力-CQ 关联 与 本体需求项。

        这两张表是**派生物**（capability_ids 与 derive_requirements 的结果），
        不该有独立事实来源；一旦某条 CQ 的主键变了，旧派生行就会指向不存在的
        CQ —— 关系表建外键时才暴露（P1-2 实测：16 关联 + 74 需求项全指向孤儿 id）。
        """
        cqs = self.store.all("cqs")
        caps = {c["id"]: c for c in self.store.all("capabilities")}
        links: dict[tuple[str, str], dict] = {}
        for cq in cqs:
            for cap_id in (cq.get("capability_ids") or []):
                if cap_id in caps:
                    links[(cap_id, cq["id"])] = {
                        "capability_id": cap_id, "cq_id": cq["id"],
                        "link_weight": 1.0}
        reqs: dict[tuple, dict] = {}
        for cq in cqs:
            for req in cap.derive_requirements(cq):
                key = (req["cq_id"], req["kind"], req["ref"])
                reqs[key] = req
        return list(links.values()), list(reqs.values())

    def derived_status(self) -> dict:
        """派生表 vs 当前 CQ 的差异（只读）：孤儿行、缺失行、总量。"""
        links, reqs = self._expected_derived()
        have_links = self.store.all("capability_cqs")
        have_reqs = self.store.all("cq_requirements")
        cq_ids = {c["id"] for c in self.store.all("cqs")}
        link_key = lambda r: (str(r.get("capability_id")), str(r.get("cq_id")))  # noqa: E731
        # 需求项按 (cq, kind, ref) 判定：cq_version 是快照标记，准入抬版本
        # 不改变需求语义，把它当键会让派生表「永远不同步」，掩盖真正的孤儿行。
        req_key = lambda r: (str(r.get("cq_id")), str(r.get("kind")),  # noqa: E731
                            str(r.get("ref")))
        want_links = {link_key(r) for r in links}
        want_reqs = {req_key(r) for r in reqs}
        have_link_keys = {link_key(r) for r in have_links}
        have_req_keys = {req_key(r) for r in have_reqs}
        orphan_links = [r for r in have_links if str(r.get("cq_id")) not in cq_ids]
        orphan_reqs = [r for r in have_reqs if str(r.get("cq_id")) not in cq_ids]
        return {
            "capability_cqs": {
                "have": len(have_link_keys), "expected": len(want_links),
                "orphan": len(orphan_links),
                "missing": len(want_links - have_link_keys),
                "stale": len(have_link_keys - want_links),
                "in_sync": not (want_links ^ have_link_keys) and not orphan_links,
            },
            "cq_requirements": {
                "have": len(have_req_keys), "expected": len(want_reqs),
                "orphan": len(orphan_reqs),
                "missing": len(want_reqs - have_req_keys),
                "stale": len(have_req_keys - want_reqs),
                "in_sync": not (want_reqs ^ have_req_keys) and not orphan_reqs,
            },
            "cqs": len(cq_ids),
            "in_sync": (not (want_links ^ have_link_keys) and not orphan_links
                        and not (want_reqs ^ have_req_keys) and not orphan_reqs),
        }

    def rebuild_derived(self, dry_run: bool = True) -> dict:
        """按当前 CQ 重建派生表；默认 dry run，落库需显式 dry_run=False。"""
        links, reqs = self._expected_derived()
        have_links = self.store.all("capability_cqs")
        have_reqs = self.store.all("cq_requirements")
        link_key = lambda r: (str(r.get("capability_id")), str(r.get("cq_id")))  # noqa: E731
        # 需求项按 (cq, kind, ref) 判定：cq_version 是快照标记，准入抬版本
        # 不改变需求语义，把它当键会让派生表「永远不同步」，掩盖真正的孤儿行。
        req_key = lambda r: (str(r.get("cq_id")), str(r.get("kind")),  # noqa: E731
                            str(r.get("ref")))
        want_links = {link_key(r): r for r in links}
        want_reqs = {req_key(r): r for r in reqs}
        have_link_map = {link_key(r): r for r in have_links}
        have_req_map = {req_key(r): r for r in have_reqs}
        plan = {
            "delete_links": sorted(set(have_link_map) - set(want_links)),
            "add_links": sorted(set(want_links) - set(have_link_map)),
            "delete_requirements": sorted(set(have_req_map) - set(want_reqs)),
            "add_requirements": sorted(set(want_reqs) - set(have_req_map)),
        }
        out = {"dry_run": bool(dry_run),
               "delete_links": len(plan["delete_links"]),
               "add_links": len(plan["add_links"]),
               "delete_requirements": len(plan["delete_requirements"]),
               "add_requirements": len(plan["add_requirements"]),
               "plan_sample": {k: [list(map(str, p)) for p in v[:3]]
                               for k, v in plan.items()}}
        if dry_run:
            return out

        def apply() -> None:
            for key in plan["delete_links"]:
                row = have_link_map[key]
                self.store.delete("capability_cqs", row["id"])
            for key in plan["delete_requirements"]:
                row = have_req_map[key]
                self.store.delete("cq_requirements", row["id"])
            for key in plan["add_links"]:
                self.store.insert("capability_cqs", dict(want_links[key]))
            for key in plan["add_requirements"]:
                self.store.insert("cq_requirements", dict(want_reqs[key]))

        db = getattr(self.store, "db", None)
        if getattr(self.store, "durable", False) and db is not None:
            with db.transaction():
                apply()
        else:
            apply()
        out["applied"] = True
        out["status"] = self.derived_status()
        self._audit("derived_rebuild",
                    {"delete_links": out["delete_links"], "add_links": out["add_links"],
                     "delete_requirements": out["delete_requirements"],
                     "add_requirements": out["add_requirements"],
                     "in_sync": out["status"]["in_sync"]}, "system")
        return out

    def registry_tracks(self) -> dict:
        """双轨对账：JSONB 副本 vs 规范化关系表（JSONB 轨事实源）。"""
        report = self.store.tracks()
        report["registry_versions"] = len(self.registry.versions)
        report["current_version"] = self.registry.current_version()
        return report

    def registry_sync_tracks(self, prune: bool = False) -> dict:
        """以 JSONB 副本为准补齐关系表（prune=True 时清理多余行）。"""
        out = self.store.sync_tracks(prune=prune)
        if out.get("written_total") is not None:
            self._audit("registry_sync_tracks",
                        {"written": out.get("written_total"),
                         "pruned": out.get("pruned"),
                         "in_sync": out.get("in_sync")}, "system")
        return out

    def governance_summary(self) -> dict:
        """对外入口：建模源与往返自证摘要（语义运行时 meta 消费）。"""
        return self._governance_summary()

    def _governance_summary(self) -> dict:
        """meta 用的轻量自证：不查库，只报建模源与往返结论。"""
        from . import linkml

        try:
            rt = linkml.roundtrip(self.registry.current())
            lossless, diff = rt["lossless"], list(rt["diff"])
        except Exception as exc:  # noqa: BLE001
            lossless, diff = False, [f"{type(exc).__name__}: {str(exc)[:120]}"]
        return {
            "linkml_single_source": True,
            "linkml_roundtrip_lossless": lossless,
            "linkml_roundtrip_diff": diff,
            "linkml_endpoints": ["/ontology/v2/schema/linkml",
                                 "/ontology/v2/schema/linkml/import",
                                 "/ontology/v2/schema/linkml/roundtrip"],
            "versions_in_registry": len(self.registry.versions),
            "current_version": self.registry.current_version(),
            "tracks_authoritative": "ontology.cq_v2_records (JSONB)",
            "tracks_mirror": "business_capabilities / capability_cqs / cq_requirements / "
                             "schema_registry / schema_migrations / schema_proposals",
            "tracks_endpoints": ["/ontology/v2/registry/tracks",
                                 "/ontology/v2/registry/tracks/sync"],
        }

    def mapping_propose(self, term: str, cq_id: str = "",
                        examples: list[str] | None = None,
                        actor: str = "llm") -> dict:
        p = mp.propose(term, self.registry, cq_id, examples, actor,
                       len(self.store.all("schema_proposals")) + 1)
        self.store.insert("schema_proposals", p)
        return p

    def proposal_approve(self, proposal_id: str, actor: str) -> dict:
        p = self.store.get("schema_proposals", proposal_id)
        if not p:
            raise LookupError("Proposal 不存在")
        patch = mp.approve(p, actor, self.registry)
        out = self.store.update("schema_proposals", proposal_id, patch)
        self._audit("proposal_approve", {"code": p.get("code")}, actor)
        return out

    def proposals(self) -> list[dict]:
        return self.store.all("schema_proposals")

    # ------------------------------------------------------------ Query Plan
    def build_plan(self, cq_id: str, permission_scopes: list[str] | None = None) -> dict:
        cq = self.get_cq(cq_id)
        if not cq:
            raise LookupError("CQ 不存在")
        plan = pl.build_plan(cq, self.registry, permission_scopes)
        check = pl.validate_plan(plan, self.registry, {"tenant_id": "system",
                                                       "scopes": permission_scopes or []})
        plan["validated"] = check["validated"]
        plan["validation_notes"] = check["notes"]
        self.store.insert("query_plans", plan)
        return plan

    def validate_plan(self, plan: dict, permission_ctx: dict) -> dict:
        return pl.validate_plan(plan, self.registry, permission_ctx)

    # ------------------------------------------------------------ 执行与评估
    def run(self, cq_id: str, snapshot: dict | None = None,
            permission_ctx: dict | None = None) -> dict:
        cq = self.get_cq(cq_id)
        if not cq:
            raise LookupError("CQ 不存在")
        snap = snapshot or self.default_snapshot(permission_ctx)
        ctx = permission_ctx or {"tenant_id": "default", "scopes": [], "actor": "system",
                                 "model_version": "local-rule-engine"}
        plan = self.build_plan(cq_id, ctx.get("scopes"))
        check = pl.validate_plan(plan, self.registry, ctx)
        if not check["validated"]:
            return {"cq_id": cq_id, "code": cq.get("code"), "blocked": True,
                    "state": "unanswerable", "reason": "查询计划校验未通过",
                    "errors": check["errors"], "plan": plan}
        expected = None
        for inst in self.store.all("instances", cq_id=cq_id):
            if inst.get("status") == "active":
                expected = inst.get("expected_answer")
                break
        result = eng.run_cq(cq, plan, snap, self.registry, expected, ctx)
        prov = result["provenance"]
        self.store.write_execution({
            "id": m.uid(), "run_id": m.uid(), "cq_id": cq_id,
            "state": result["state"], "plan_version": prov["plan_version"],
            "schema_version": prov["schema_version"], "cq_version": prov["cq_version"],
            "data_snapshot_id": prov["data_snapshot_id"],
            "model_version": ctx.get("model_version"), "tenant_id": ctx.get("tenant_id"),
            "actor": ctx.get("actor"),
            "answer": {"values": result["values"]},
            "evidence_refs": prov["evidence_refs"],
            "conflicts": result["validation"]["conflicts"],
            "audit": {"plan": plan["plan_version"], "gaps": len(result["gaps"])},
        })
        return result

    def default_snapshot(self, permission_ctx: dict | None = None) -> dict:
        """#20：默认用真实数据快照；仅当无库（单测/离线，或 CQ_V2_SNAPSHOT=demo）时回落演示数据。"""
        import os
        if os.getenv("CQ_V2_SNAPSHOT", "live").lower() == "demo":
            return demo_snapshot()
        try:
            from app.modules.ontology.live_snapshot import build
            ctx = permission_ctx or {}
            return build(tenant_id=str(ctx.get("tenant_id") or "default"),
                         principal=ctx.get("principal"), registry=self.registry)
        except Exception as exc:  # noqa: BLE001
            snap = demo_snapshot()
            snap["fallback_reason"] = f"live snapshot unavailable: {type(exc).__name__}"
            return snap

    def coverage_run(self, only_admitted: bool = True, snapshot: dict | None = None,
                     permission_ctx: dict | None = None) -> dict:
        cqs = [c for c in self.cqs() if (not only_admitted or q.admitted(c))]
        snap = snapshot or self.default_snapshot(permission_ctx)
        items, gaps = [], []
        for cq in cqs:
            r = self.run(cq["id"], snap, permission_ctx)
            if r.get("blocked"):
                continue
            items.append(r)
            gaps.extend(r["gaps"])
        run_id = m.uid()
        self.store.write_coverage(items, gaps, run_id)
        weighted = eng.weighted_coverage(items)
        gates = eng.layered_gates(items)
        states = {s: sum(1 for i in items if i["state"] == s) for s in m.ANSWER_STATES}
        cap_rows = cap.capability_coverage(self.capabilities(),
                                           self.store.all("capability_cqs"),
                                           {i["cq_id"]: i["state"] for i in items})
        hr = cap.high_risk_gate(cap_rows, [
            {"capability_id": c["capability_id"],
             "accuracy": 1.0 if c["coverage"] >= 0.8 else 0.5,
             "evidence_rate": 1.0 if not c["has_conflict"] else 0.4}
            for c in cap_rows])
        out = {
            "run_id": run_id, "run_at": m.now(), "only_admitted": only_admitted,
            "total": len(items), "states": states, "weighted": weighted,
            "gates": gates, "high_risk_gate": hr,
            "capability_coverage": cap_rows,
            "gap_count": len(gaps), "gap_kinds": {
                k: sum(1 for g in gaps if g["kind"] == k) for k in m.GAP_KINDS},
            "top_gaps": sorted(gaps, key=lambda g: g["gap_priority"], reverse=True)[:10],
            "items": items,
            "data_snapshot_id": snap.get("data_snapshot_id"),
            "schema_version": self.registry.current_version(),
        }
        self._audit("coverage_run", {"run_id": run_id, "total": len(items)})
        return out

    def gaps(self) -> list[dict]:
        rows = self.store.all("cq_gaps")
        return sorted(rows, key=lambda g: g.get("gap_priority", 0), reverse=True)

    # ------------------------------------------------------------ 决策型 CQ
    def decision_run(self, cq_id: str, evidence_ctx: dict | None = None,
                     rules: list[dict] | None = None) -> dict:
        cq = self.get_cq(cq_id)
        if not cq:
            raise LookupError("CQ 不存在")
        if cq.get("intent") not in m.DECISION_INTENTS:
            raise ValueError("仅 decision / recommendation 类型 CQ 可进入决策评估层")
        out = dec.evaluate(cq, evidence_ctx, rules)
        self.store.insert("decisions", out)
        return out

    def decision_approve(self, decision_id: str, actor: str,
                         decision_value: str | None = None,
                         authorized_execution: bool = False) -> dict:
        d = self.store.get("decisions", decision_id)
        if not d:
            raise LookupError("决策记录不存在")
        patch = dec.approve(d, actor, decision_value, authorized_execution)
        self._audit("decision_approve", {"id": decision_id}, actor)
        return self.store.update("decisions", decision_id, patch)

    # ------------------------------------------------------------ 集合治理与演进
    def portfolio(self) -> dict:
        return evo.portfolio_quality(
            self.cqs(), self.capabilities(), self.store.all("capability_cqs"),
            self.store.all("cq_requirements"), self.registry)

    def evolution_cycle(self, actor: str = "业务负责人") -> dict:
        baseline_before = dict(self.baseline)
        last: dict = {}

        handlers = {
            "define_capabilities": lambda: {
                "capabilities": len(self.capabilities()), "cqs": len(self.cqs()),
                "at": m.now()},
            "derive_requirements": lambda: {
                "requirements": len(self.store.all("cq_requirements")),
                "layers": self.four_layers()},
            "build_knowledge": lambda: {
                "schema_version": self.registry.current_version(),
                "linkml_ready": bool(self.registry_linkml())},
            "run_cq_tests": lambda: self._loop_coverage(last),
            "analyze_gaps": lambda: {"summary": last.get("gap_kinds", {}),
                                     "top": last.get("top_gaps", [])[:3]},
            "generate_proposals": lambda: self._loop_proposals(last),
            "re_evaluate": lambda: {"baseline_after": last.get("baseline_after", {})},
        }
        out = evo.run_loop(handlers, actor)
        out["baseline_before"] = baseline_before
        self.baseline = last.get("baseline_after", {})
        self._audit("evolution_cycle", {"run_id": out["run_id"]}, actor)
        self.store.insert("evolution_runs", out)
        return out

    def _loop_coverage(self, last: dict) -> dict:
        r = self.coverage_run(only_admitted=True)
        last.update({"gap_kinds": r["gap_kinds"], "top_gaps": r["top_gaps"]})
        last["baseline_after"] = {
            "coverage": r["weighted"]["coverage"],
            "evidence_rate": r["gates"]["evidence_traceability"],
            "high_risk_accuracy": r["high_risk_gate"]["accuracy"],
            "states": r["states"],
        }
        return {"coverage": r["weighted"], "gates": r["gates"],
                "high_risk_accuracy": r["high_risk_gate"]["accuracy"],
                "evidence_rate": r["gates"]["evidence_traceability"],
                "baseline_before": dict(self.baseline),
                "baseline_after": last["baseline_after"]}

    def _loop_proposals(self, last: dict) -> dict:
        count = 0
        for kind, n in (last.get("gap_kinds") or {}).items():
            if kind == "schema" and n > 0:
                self.mapping_propose("新业务概念", examples=["例句1", "例句2", "例句3"])
                count += 1
        return {"count": count, "note": "只生成 proposal，不自动发布 Schema"}

    # ------------------------------------------------------------ 指标与审计
    def metrics(self) -> list[dict]:
        """内存版 v_cq_metrics_v2：三个指标分列，互不扣分。"""
        coverage = self.store.all("cq_coverage_v2")
        evaluation = self.store.all("cq_evaluation_runs")
        out = []
        for c in self.cqs():
            cov = [r for r in coverage if r.get("cq_id") == c["id"]]
            eva = [r for r in evaluation if r.get("cq_id") == c["id"]]
            out.append({
                "id": c["id"], "code": c["code"], "question": c["question"],
                "version": c.get("version"),
                "quality_score": c.get("quality_score"),
                "grade": m.grade_of(c.get("quality_score")),
                "admitted": q.admitted(c),
                "review_status": c.get("review_status"),
                "risk_level": c.get("risk_level"),
                "intent": c.get("intent"),
                "business_domain": c.get("business_domain"),
                "layer_states": c.get("layer_states"),
                "answerability_state": cov[-1].get("state") if cov else None,
                "answerability_score": cov[-1].get("weighted_score") if cov else None,
                "answer_accuracy": eva[-1].get("accuracy") if eva else None,
                "answer_evidence_rate": eva[-1].get("evidence_rate") if eva else None,
            })
        return out

    def audit(self) -> list[dict]:
        return list(self.audit_log)
