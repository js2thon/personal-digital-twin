"""本体节点类型契约（供指标编译与兼容解析使用）（JSON Schema Draft-07）。

设计原则（与用户确认的方案一致）：
- 建模即流程：本体定义是一条可反复运行的工作流，不是静态图。
- 画布只保留 7 类建模设计节点，覆盖 Schema / Instance / Semantic / Executable 四层。
- Schema 映射能力内化到「属性 / 类型」节点（source_field / is_pk / target_entity / source_fields）。
- 实例化写入 / 完整验证 / 发布版本不属于画布 pipeline，移到「完整验证」后的收尾操作区；
  发布门禁按四类完整验证报告判定，不再依赖画布上的 publish 节点。
- 最小完备集：7 类节点按执行顺序固定，可扩展不可减少。

本模块保留节点类型和旧 kind 归一化契约，供指标编译器兼容解析使用。

无外部依赖，纯数据 + 纯函数。
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- 四层覆盖
LAYERS: list[dict[str, str]] = [
    {"id": "schema", "label": "Schema 层", "desc": "实体、属性、关系、约束（结构定义）"},
    {"id": "instance", "label": "Instance 层", "desc": "数据导入、探查清洗、实例化写入配置"},
    {"id": "semantic", "label": "Semantic 层", "desc": "Function、Rule、派生计算"},
    {"id": "executable", "label": "Executable 层", "desc": "Action、Monitor、可触发行为"},
]

# ---------------------------------------------------------------- 7 类建模设计节点
# order = 画布执行顺序；required = 该字段必填；layer = 归属层
NODE_TYPES: dict[str, dict[str, Any]] = {
    "datasource": {
        "order": 1, "label": "数据源节点", "icon": "▤", "layer": "instance",
        "desc": "数据接入，支持文件 / API / MySQL / Oracle / PostgreSQL 多源并行",
        "required": ["name", "source_type"],
        "fields": {
            "name": {"type": "string", "title": "节点名称"},
            "source_type": {"type": "string", "enum": ["file", "api", "mysql", "oracle", "postgres", "other"], "title": "数据源类型"},
            "file_name": {"type": "string", "title": "文件名"},
            "file_format": {"type": "string", "enum": ["csv", "xlsx", "json", "txt"], "default": "csv"},
            "file_sheet": {"type": "string", "title": "工作表"},
            "api_url": {"type": "string", "title": "接口 URL"},
            "api_method": {"type": "string", "enum": ["GET", "POST", "PUT"], "default": "GET"},
            "api_auth": {"type": "string", "enum": ["none", "apikey", "oauth", "basic"], "default": "none"},
            "api_paging": {"type": "string", "title": "分页策略"},
            "db_host": {"type": "string", "title": "主机"},
            "db_port": {"type": "string", "title": "端口"},
            "db_name": {"type": "string", "title": "库名"},
            "db_schema": {"type": "string", "title": "Schema"},
            "db_table": {"type": "string", "title": "表/视图"},
            "db_user": {"type": "string", "title": "只读账号"},
            "source": {"type": "string", "title": "业务口径来源"},
            "cadence": {"type": "string", "title": "采样周期"},
            "note": {"type": "string", "title": "说明"},
        },
    },
    "profiler": {
        "order": 2, "label": "数据探查 & 清洗节点", "icon": "⚙", "layer": "instance",
        "desc": "对采样数据做探查（空值率/唯一性/分布/类型）与清洗（归一/去噪/补全）",
        "required": ["name", "clean_rules"],
        "fields": {
            "name": {"type": "string", "title": "节点名称"},
            "input": {"type": "string", "title": "输入来源"},
            "profile_rules": {"type": "string", "title": "探查项（逗号分隔：null/unique/distribution/type）"},
            "clean_rules": {
                "type": "string",
                "title": "清洗规则（每行一条 op(field, key=value)，# 后为注释）",
                "description": "如 dedup(employee_no, keep=first) 按字段去重保留首条；"
                               "支持 trim/drop_null/normalize_date/map_values/fill_default/lower/upper/regex_replace。",
            },
            "output": {"type": "string", "title": "输出"},
            # —— 数据血缘（画布「数据处理 → 业务对象」手工/自动关联的唯一登记处）——
            # 处理节点的产出喂给哪些业务对象。值可以是对象节点 id，也可以是对象名；
            # 画布上的 process→object 虚线由它渲染，自动连线反向写入，两处口径始终一致。
            "link_objects": {
                "type": "array", "items": {"type": "string"},
                "title": "关联业务对象（处理结果喂给哪些对象）",
                "description": "对象节点 id 或对象名列表；为空表示这份处理结果没有下游，"
                               "验证会给出 PROCESS_UNLINKED 提示（不阻断发布）。",
            },
            "link_mode": {
                "type": "string", "enum": ["manual", "auto"], "default": "auto",
                "title": "血缘登记方式",
                "description": "manual = 用户在详情面板勾选过；auto = 自动连线按流水线推断写入。",
            },
        },
    },
    "entity": {
        "order": 3, "label": "实体定义节点", "icon": "▣", "layer": "schema",
        "desc": "Entity + 主键 + 属性 + 类型系统；Schema 字段→属性映射内化在属性/类型节点",
        "required": ["name", "primary_key"],
        "fields": {
            "name": {"type": "string", "title": "实体名（英文标识符）", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
            "label": {"type": "string", "title": "显示名"},
            "primary_key": {"type": "string", "title": "主键属性"},
            "attributes": {"type": "string", "title": "属性定义（每行 名称:类型[:必填][:枚举/正则]）"},
            "type_system": {"type": "string", "title": "类型系统（父类型 / 约束）"},
            "source_object": {"type": "string", "title": "对应真实实体"},
            "aliases": {"type": "string", "title": "别名（逗号分隔）"},
            # —— 内化的 Schema 映射字段（旧 schema_mapping 节点的能力）——
            "source_field": {"type": "string", "title": "来源字段（属性级：本属性来自哪个源字段）"},
            "is_pk": {"type": "boolean", "title": "是否主键（属性级标记）", "default": False},
            "target_entity": {"type": "string", "title": "目标实体（类型级：映射到语义层实体名）"},
            "source_fields": {"type": "string", "title": "源字段清单（类型级：逗号分隔）"},
        },
    },
    "relation": {
        "order": 4, "label": "关系定义节点", "icon": "↔", "layer": "schema",
        "desc": "Edge + 基数 + 方向 + 属性边",
        "required": ["name", "source_entity", "target_entity", "cardinality"],
        "fields": {
            "name": {"type": "string", "title": "关系名（英文标识符）"},
            "source_entity": {"type": "string", "title": "源端实体"},
            "target_entity": {"type": "string", "title": "目标端实体"},
            "cardinality": {"type": "string", "enum": ["1:1", "1:N", "N:M"], "title": "基数"},
            "directed": {"type": "boolean", "title": "有向", "default": True},
            "edge_properties": {"type": "string", "title": "属性边（逗号分隔）"},
        },
    },
    "constraint": {
        "order": 5, "label": "约束 & 规则节点", "icon": "⊘", "layer": "schema",
        "desc": "唯一性、范围、引用完整性、业务规则（Schema 约束 + Semantic 规则）",
        "required": ["name", "kind", "target", "expression"],
        "fields": {
            "name": {"type": "string", "title": "约束名"},
            "kind": {"type": "string", "enum": ["unique", "range", "referential", "business"], "title": "约束类型"},
            "target": {"type": "string", "title": "作用目标（实体.属性）"},
            "expression": {"type": "string", "title": "约束表达式"},
            "severity": {"type": "string", "enum": ["error", "warn"], "default": "error", "title": "级别"},
        },
    },
    "function": {
        "order": 6, "label": "Function 节点", "icon": "ƒ", "layer": "semantic",
        "desc": "可计算属性 / 派生指标 / 规则函数",
        "required": ["name", "output", "logic"],
        "fields": {
            "name": {"type": "string", "title": "函数名（英文标识符）"},
            "kind": {"type": "string", "enum": ["derive", "aggregate", "rule", "utility"], "default": "derive"},
            "inputs": {"type": "string", "title": "输入属性（逗号分隔）"},
            "output": {"type": "string", "title": "输出属性"},
            "logic": {"type": "string", "title": "逻辑描述（声明式）"},
            "stateful": {"type": "boolean", "title": "有状态（事件触发）", "default": False},
        },
    },
    "action": {
        "order": 7, "label": "Action / Monitor 节点", "icon": "⚡", "layer": "executable",
        "desc": "可执行行为与监控：事件/条件/定时/人工触发 + 回调写回",
        "required": ["name", "effect"],
        "fields": {
            "name": {"type": "string", "title": "行动名"},
            "role": {"type": "string", "enum": ["action", "monitor"], "default": "action", "title": "角色"},
            "trigger": {"type": "string", "enum": ["event", "condition", "schedule", "manual"], "default": "event"},
            "condition": {"type": "string", "title": "触发条件"},
            "effect": {"type": "string", "title": "执行动作 / 回调"},
            "cadence": {"type": "string", "title": "执行力度"},
            "closed_loop": {"type": "boolean", "title": "闭环回写本体", "default": True},
        },
    },
}

# 画布执行顺序（7 类建模设计节点）
WORKFLOW_ORDER: list[str] = [k for k, _ in sorted(NODE_TYPES.items(), key=lambda kv: kv[1]["order"])]
# 画布无强制节点；发布门禁改按四类完整验证报告判定
MANDATORY_KINDS: tuple[str, ...] = ()

# 旧草稿 kind → 标准 kind 的兼容映射（前端 mdNormalizeSpec 与后端共用语义）
# 旧 schema_mapping / instantiate / validation / publish 已退出画布，统一归一到 entity。
LEGACY_KIND_MAP: dict[str, str] = {
    "sample": "datasource", "process": "profiler", "object": "entity",
    "attr": "entity", "type": "entity", "func": "function", "action": "action",
    "mapping": "entity", "schema_mapping": "entity",
    "instantiate": "entity", "validation": "entity", "publish": "entity",
}


def kind_of(raw: str | None) -> str | None:
    """把任意 kind（标准或旧版别名）归一到标准 kind；未知返回 None。"""
    if not raw:
        return None
    if raw in NODE_TYPES:
        return raw
    return LEGACY_KIND_MAP.get(raw)


def node_json_schema() -> dict[str, Any]:
    """生成画布工作流 JSON Schema（Draft-07），供前端配置面板与契约校验共用。"""
    kind_props = {}
    for key, spec in NODE_TYPES.items():
        fields = {}
        for fname, fdef in spec["fields"].items():
            f = {"type": fdef["type"], "title": fdef.get("title", fname)}
            if fdef.get("description"):
                f["description"] = fdef["description"]
            if "enum" in fdef:
                f["enum"] = fdef["enum"]
            if "default" in fdef:
                f["default"] = fdef["default"]
            fields[fname] = f
        kind_props[key] = {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "kind": {"const": key},
                "fields": {"type": "object", "properties": fields,
                           "required": list(spec.get("required") or [])},
            },
        }
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://local/ontology/node-contract.schema.json",
        "title": "本体建模工作流（7 类建模设计节点）",
        "description": "画布 7 类节点覆盖 Schema/Instance/Semantic/Executable 四层；"
                       "Schema 映射内化到属性/类型；实例化/验证/发布在收尾区，按验证报告判定门禁",
        "type": "object",
        "properties": {
            "version": {"type": "string"},
            "nodes": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": WORKFLOW_ORDER + list(LEGACY_KIND_MAP)},
                },
            }},
            "edges": {"type": "array", "items": {"type": "object"}},
            # 收尾区配置：实例化与发布（不属于画布节点）
            "instantiate": {"type": "object", "title": "实例化写入配置（收尾区）"},
            "publish": {"type": "object", "title": "发布配置（收尾区）"},
        },
        "x-node-types": NODE_TYPES,
        "x-layers": LAYERS,
        "x-workflow-order": WORKFLOW_ORDER,
        "x-mandatory-kinds": list(MANDATORY_KINDS),
        "x-note": "发布门禁按四类完整验证报告判定，不依赖画布节点",
    }


def contract_summary() -> dict[str, Any]:
    """给前端的精简契约摘要（渲染节点面板用，避免拉整个 schema）。"""
    return {
        "order": WORKFLOW_ORDER,
        "layers": LAYERS,
        "kinds": {k: {"order": v["order"], "label": v["label"], "icon": v["icon"],
                      "layer": v["layer"], "desc": v["desc"], "required": v["required"]}
                  for k, v in NODE_TYPES.items()},
        "legacy_map": LEGACY_KIND_MAP,
        "mandatory": list(MANDATORY_KINDS),
    }
