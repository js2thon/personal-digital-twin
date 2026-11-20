"""统一 Schema Registry：LinkML 单一建模来源 + 版本/迁移/回滚/版本锁定。

架构位置：Ontology Schema Registry → Schema Validator → Runtime Metadata
→ Projection Engine → CQ Planner。关系目录不再是 graph.py 里的代码常量。
"""
from __future__ import annotations

from . import models as m


def default_schema(version: str = "v2.0.0") -> dict:
    """初始 Schema：与 V1.2 已发布本体对齐，relations 段补齐。"""
    entity_types = {
        "Person": {"label": "人员", "aliases": ["员工", "人"], "parent": ""},
        "OrganizationUnit": {"label": "组织单元",
                             "aliases": ["部门", "组织", "事业部", "业务单元"],
                             "parent": ""},
        "Project": {"label": "项目", "aliases": ["项目组"], "parent": ""},
        "Policy": {"label": "制度", "aliases": ["规则", "办法"], "parent": ""},
        "Document": {"label": "文档", "aliases": ["文件", "材料"], "parent": ""},
        "System": {"label": "系统", "aliases": ["平台"], "parent": ""},
        "Risk": {"label": "风险", "aliases": ["风险项"], "parent": ""},
        "Skill": {"label": "技能", "aliases": ["能力"], "parent": ""},
    }
    relation_types = {
        "reports_to": {"label": "汇报对象", "source": "Person", "target": "Person",
                       "cardinality": "many_to_one"},
        "works_at": {"label": "任职于", "source": "Person",
                     "target": "OrganizationUnit", "cardinality": "many_to_one"},
        "member_of": {"label": "成员属于", "source": "Person",
                      "target": "OrganizationUnit", "cardinality": "many_to_many"},
        "is_project_manager_of": {"label": "负责项目", "source": "Person",
                                  "target": "Project", "cardinality": "one_to_many"},
        "works_on": {"label": "参与项目", "source": "Person", "target": "Project",
                     "cardinality": "many_to_many"},
        "collaborates_with": {"label": "协作", "source": "Person", "target": "Person",
                              "cardinality": "many_to_many"},
        "has_role": {"label": "担任角色", "source": "Person", "target": "OrganizationUnit",
                     "cardinality": "many_to_one"},
        "authored": {"label": "撰写", "source": "Person", "target": "Document",
                     "cardinality": "one_to_many"},
        "references": {"label": "引用", "source": "Document", "target": "Document",
                       "cardinality": "many_to_many"},
        "believes": {"label": "持有观点", "source": "Person", "target": "Document",
                     "cardinality": "one_to_many"},
        "knows": {"label": "掌握技能", "source": "Person", "target": "Skill",
                  "cardinality": "many_to_many"},
        "owns": {"label": "负责系统", "source": "Person", "target": "System",
                 "cardinality": "many_to_one"},
    }
    attributes = {
        "level": {"range": "string", "owner": "Person"},
        "job_title": {"range": "string", "owner": "Person"},
        "due_date": {"range": "date", "owner": "Project"},
        "risk_level": {"range": "string", "owner": "Risk"},
        "approval_status": {"range": "string", "owner": "Project"},
    }
    return {
        "schema_version": version, "entity_types": entity_types,
        "relation_types": relation_types, "attributes": attributes,
        "constraints": {"max_hops": m.MAX_HOPS, "single_valued": ["level", "due_date"]},
        "validation_rules": {"R2": "关系两端类型必须满足 source/target 约束"},
        "status": "published", "pinned_by_cq": [], "published_by": "system",
        "created_at": m.now(),
    }


def diff(a: dict | None, b: dict) -> dict:
    """Schema Diff：新增/删除/变更，按类型分节。

    `validation_rules` 也在比较范围内：只增规则不改结构，同样是 Schema 变更，
    漏掉它 diff 会显示「无变化」，发布记录就失真。
    """
    out: dict[str, dict] = {}
    a = a or {}
    for section in ("entity_types", "relation_types", "attributes", "constraints",
                    "validation_rules"):
        old, new = m.as_dict(a.get(section)), m.as_dict(b.get(section))
        keys = set(old) | set(new)
        out[section] = {
            "added": sorted(k for k in keys if k in new and k not in old),
            "removed": sorted(k for k in keys if k in old and k not in new),
            "changed": sorted(k for k in keys
                              if k in old and k in new and old[k] != new[k]),
        }
    return out


def compatibility(d: dict) -> str:
    for section in d.values():
        if section.get("removed") or section.get("changed"):
            return "breaking"
    return "compatible"


def migration_plan(d: dict, from_version: str, to_version: str) -> list[dict]:
    steps = []
    for section, ch in d.items():
        for key in ch.get("added", []):
            steps.append({"op": "add", "section": section, "ref": key,
                          "note": "登记新定义，历史数据不受影响"})
        for key in ch.get("removed", []):
            steps.append({"op": "deprecate", "section": section, "ref": key,
                          "note": "先停用再迁移，禁止直接删除历史事实"})
        for key in ch.get("changed", []):
            steps.append({"op": "migrate_data", "section": section, "ref": key,
                          "note": "需要数据迁移与规则复检（R2 类型约束）"})
    steps.append({"op": "validate", "section": "*", "ref": to_version,
                  "note": "发布前跑 Schema Validator + CQ 回归"})
    return steps


def to_linkml(schema: dict) -> str:
    """LinkML YAML 单一建模来源。

    序列化实现在 `linkml.py`（与解析器同文件，保证互为逆运算）；这里保留同名
    入口，避免调用方感知模块拆分。
    """
    from . import linkml

    return linkml.dumps(schema)


class SchemaRegistry:
    """纯逻辑实现；持久化由 Store 的 KV 承载（to_state/load_state 往返无损）。"""

    def __init__(self, initial: dict | None = None):
        self.versions: dict[str, dict] = {}
        self.migrations: list[dict] = []
        self.pins: dict[str, str] = {}
        self.order: list[str] = []
        if initial:
            self.versions[initial["schema_version"]] = dict(initial)
            self.order.append(initial["schema_version"])

    # ------------------------------------------------------------ 状态序列化
    def to_state(self) -> dict:
        """完整快照：版本表 / 发布顺序 / 迁移记录 / CQ 锁定，缺一项重启就漂移。"""
        return {"versions": self.versions, "order": list(self.order),
                "migrations": self.migrations, "pins": dict(self.pins)}

    def load_state(self, state: dict | None) -> bool:
        """从快照恢复；空快照返回 False（保留当前默认 Schema，不置空）。"""
        state = state or {}
        versions = state.get("versions") or {}
        order = [v for v in (state.get("order") or []) if v in versions]
        if not versions or not order:
            return False
        self.versions = {v: dict(versions[v]) for v in order}
        self.order = order
        self.migrations = list(state.get("migrations") or [])
        self.pins = {str(k): v for k, v in (state.get("pins") or {}).items()}
        return True

    def current_version(self) -> str:
        return self.order[-1] if self.order else ""

    def current(self) -> dict:
        v = self.current_version()
        return self.versions.get(v, {})

    def get(self, version: str) -> dict:
        return self.versions.get(version, {})

    def publish(self, schema: dict, actor: str = "owner") -> dict:
        ver = schema.get("schema_version") or f"v{len(self.order) + 1}.0.0"
        if ver in self.versions:
            raise ValueError(f"Schema 版本已存在：{ver}")
        old = self.current()
        d = diff(old, schema)
        comp = compatibility(d)
        plan = migration_plan(d, self.current_version(), ver)
        rec = {
            "id": m.uid(), "from_version": self.current_version(), "to_version": ver,
            "diff": d, "migration_plan": plan, "compatibility": comp,
            "data_migration": {"steps": [s for s in plan if s["op"] == "migrate_data"]},
            "rollback_sql": f"-- rollback to {self.current_version()}",
            "rollback_version": self.current_version(),
            "applied_at": m.now(), "applied_by": actor,
        }
        schema = {**schema, "schema_version": ver, "status": "published",
                  "published_by": actor, "created_at": m.now()}
        self.versions[ver] = schema
        self.order.append(ver)
        self.migrations.append(rec)
        return {"schema_version": ver, "diff": d, "compatibility": comp,
                "migration_plan": plan, "migration": rec}

    def rollback(self, version: str, actor: str = "owner") -> dict:
        if version not in self.versions:
            raise LookupError(f"Schema 版本不存在：{version}")
        if self.current_version() == version:
            return {"rolled_back_to": version, "changed": False}
        rec = {"id": m.uid(), "from_version": self.current_version(),
               "to_version": version, "diff": diff(self.current(), self.versions[version]),
               "migration_plan": [{"op": "rollback", "ref": version}],
               "compatibility": "breaking", "data_migration": {},
               "rollback_sql": "-- manual verification required",
               "applied_at": m.now(), "applied_by": actor}
        self.order.append(version)
        self.migrations.append(rec)
        return {"rolled_back_to": version, "changed": True, "migration": rec}

    def pin(self, cq_id: str, version: str) -> dict:
        if version not in self.versions:
            raise LookupError(f"Schema 版本不存在：{version}")
        self.pins[cq_id] = version
        cur = self.versions[version]
        pins = list(cur.get("pinned_by_cq") or [])
        if cq_id not in pins:
            pins.append(cq_id)
        cur["pinned_by_cq"] = pins
        return {"cq_id": cq_id, "schema_version": version}

    def validate_ref(self, kind: str, ref: str) -> bool:
        cur = self.current()
        section = {"entity": "entity_types", "relation": "relation_types",
                   "attribute": "attributes"}.get(kind)
        return bool(section and ref in (cur.get(section) or {}))
