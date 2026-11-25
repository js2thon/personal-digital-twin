"""本体运行时 · 编译器：建模草稿 spec → 可执行 IR（含体检结论）。

职责边界：**只做翻译与体检，不做计算**（计算在 engine.py，算法在 metrics.py）。
把画布那套「前端 5 段流水线 + 后端标准 kind」的草稿，翻译成运行时能直接执行的结构：
对象表 / 属性表 / 函数三型 / 关系 / 约束，并对每一项给出「可执行 or 为什么不可执行」。

这是全项目唯一的「本体可执行性」口径来源：
- engine.py 拿 IR 执行；
- assistant.py 拿 IR 的体检结论回答「能算出哪些指标」「哪个函数不可执行」；
- MCP 的 ontology_functions / ontology_metrics / ontology_capabilities 也读同一份。

关键判定（严格照实，不做善意猜测）：

1. **对象 vs 属性**（最容易踩的坑）：前端画布上的 ``attr`` 节点提交后 kind 也是
   ``entity``（见 ``web/index.html`` 的 ``modNormalizeSpec``），所以不能只看 kind。
   判定优先级：① 有来自另一个 entity 节点的入边（``object → attr`` 边，画布就是这么连的）；
   ② 带属性专属字段（``range`` / ``metric_fn`` / ``type_src`` / ``enum`` / ``pattern``）；
   ③ 名字出现在某个对象的 ``attributes`` 声明里。三条都不满足才是业务对象。
2. **属性归属**：``object → attr`` 边 → ``fields.object/parent/owner_object`` →
   唯一对象推断（出 ``ATTR_OWNER_INFERRED`` info）→ 否则进 ``unassigned_attrs``
   并出 ``ATTR_NO_OBJECT`` 警告。**不默认塞给第一个对象**。
3. **函数归属**：``attr → func`` 边 → ``fields.owner_attr``（id 或名字）→
   ``fields.inputs`` 与属性名相等。三档都失败即「孤儿函数」。
4. **指标口径**：属性上选了 ``metric_fn`` → 直接用；否则看函数 ``fields.agg``；再否则
   从 ``logic`` 文本识别可确定的口径（count distinct / 非空率 / sum / avg / group by /
   count(*)…）。识别不出就判不可执行，**不默认 Sum**。
5. **派生口径**：只认三种可求值写法——``out = expr``、``if cond then a else b``、
   ``整个 logic 就是一个表达式``。自然语言描述一律判不可执行并给出改写提示
   （前端 26 条字段族规则生成的 logic 基本全是中文自然语言，见 ``MD_FN_RULES``）。
6. **行动口径**：``condition`` 必须能当布尔表达式求值；``effect`` 是声明式文本，
   运行时只判触发并留痕，不执行外部副作用（IR 里显式标 ``effect_mode="declared"``）。

全部纯函数，零 I/O。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..node_contract import NODE_TYPES, WORKFLOW_ORDER, kind_of
from . import expr as EX
from .metrics import ALL_ALGOS, METRIC_ALGOS, algo_label, is_official

# ---------------------------------------------------------------- 常量
FN_TYPES: tuple[str, ...] = ("metric", "derive", "action")
FN_TYPE_LABEL: dict[str, str] = {
    "metric": "指标函数", "derive": "派生函数", "action": "行动函数",
}
# 后端 function.kind enum → 前端三型（前端 modNormalizeSpec 提交的就是左边这些值）
_STD_TO_TYPE: dict[str, str] = {
    "aggregate": "metric", "metric": "metric",
    "derive": "derive", "utility": "derive",
    "rule": "action", "action": "action",
}
BASE_RANGES: tuple[str, ...] = (
    "string", "integer", "number", "boolean", "date", "datetime", "enum",
)
CONSTRAINT_KINDS: tuple[str, ...] = ("unique", "range", "referential", "business")

ERR = "error"
WARN = "warn"
INFO = "info"

# ---------------------------------------------------------------- 正则
_ASSIGN_RE = re.compile(
    r"^([A-Za-z_\u4e00-\u9fa5][\w\u4e00-\u9fa5]*)\s*=(?!=)\s*(.+)$", re.S)
_IF_RE = re.compile(
    r"^if\s+(.+?)\s+then\s+(.+?)(?:\s+else\s+(.+))?$", re.I | re.S)
_GROUP_BY_RE = re.compile(r"group\s+by\s+([^\s,，;；()（）]+)", re.I)
_SPECIAL_RE = re.compile(r"^(unique|not_null|notnull|matches|in_range|in)\s*\((.*)\)$",
                         re.I | re.S)
_REFER_RE = re.compile(r"^references?\s*\(\s*([\w.]+)\s*=\s*([\w.]+)\s*\)$", re.I | re.S)
_ORDER_BY_RE = re.compile(r"order\s*by\s+([A-Za-z_][\w]*)", re.I)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 属性专属字段：出现任意一个（非空）就说明这个 entity 节点其实是属性
_ATTR_ONLY_KEYS = ("range", "metric_fn", "type_src", "enum", "pattern")
_SPLIT_CHARS = ",，;；、|\t\n"

# ---------------------------------------------------------------- 语义映射字段
# 「业务语义 → 事实库物理字段」的映射挂在建模产物上，是问数可推导的根。
# 属性节点 fields：
#   fact_predicate : 该属性在 knowledge.facts 里对应的 predicate（如 status）
#   fact_value     : 该属性某个语义取值对应的 facts.object_value.value（如 A）
#   value_meaning  : 该取值的中文语义标签（如 在职）——问句「在职」据此命中
# 对象节点 fields：
#   fact_entity_type : 该对象在 ontology.entities.entity_type 里的类型名（如 Person）
# 全部为可选；缺任一即该指标「不可从本体推导」，aq 如实拒答，绝不回退硬编码。
_SEM_ATTR_KEYS = ("fact_predicate", "fact_value", "value_meaning")
_SEM_OBJECT_KEY = "fact_entity_type"


# ---------------------------------------------------------------- 小工具
def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return _s(v).lower() in ("1", "true", "yes", "y", "on", "是", "必填", "required")


def _split_list(text: Any) -> list[str]:
    """拆清单：中英文逗号、顿号、分号、竖线、tab 都算分隔符；括号/引号内不拆。

    已结构化的数组/元组按元素取用——直接 str(list) 会把整份清单变成一个元素
    （"['在职', '离职']"），枚举在图上就只剩一个个体。逐元素去掉包裹的引号。
    """
    if isinstance(text, (list, tuple, set)):
        out: list[str] = []
        for item in text:
            v = _s(item).strip().strip("'\"").strip()
            if v:
                out.append(v)
        return out
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    for ch in _s(text):
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            continue
        if ch in "([{（【":
            depth += 1
            buf.append(ch)
            continue
        if ch in ")]}）】":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if ch in _SPLIT_CHARS and depth == 0:
            s = "".join(buf).strip()
            if s:
                out.append(s)
            buf = []
            continue
        buf.append(ch)
    s = "".join(buf).strip()
    if s:
        out.append(s)
    return out


def _norm_nodes(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """归一节点：kind 统一到标准；fields 兼容旧草稿的顶层字段写法（与 validator 同口径）。"""
    out: list[dict[str, Any]] = []
    for n in (spec or {}).get("nodes") or []:
        if not isinstance(n, dict):
            continue
        kind = kind_of(n.get("kind"))
        fields = dict(n.get("fields") or {})
        for k in ("name", "label", "source_type", "range", "required", "cardinality",
                  "primary_key", "attributes", "inputs", "output", "logic"):
            if k not in fields and k in n:
                fields[k] = n[k]
        out.append({**n, "kind": kind, "fields": fields})
    return out


def _norm_edges(spec: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for e in (spec or {}).get("edges") or []:
        if not isinstance(e, dict):
            continue
        a, b = _s(e.get("from") or e.get("source")), _s(e.get("to") or e.get("target"))
        if a and b:
            out.append({"from": a, "to": b, "id": _s(e.get("id"))})
    return out


def spec_hash(spec: dict[str, Any]) -> str:
    """草稿指纹：只对 nodes/edges 的内容做规范化哈希，与顺序、坐标、选中态无关。"""
    nodes = sorted(
        ({"kind": n.get("kind"), "fields": n.get("fields") or {}}
         for n in (spec or {}).get("nodes") or [] if isinstance(n, dict)),
        key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True, default=str),
    )
    edges = sorted(
        (tuple(sorted((_s(e.get("from") or e.get("source")),
                       _s(e.get("to") or e.get("target")))))
         for e in (spec or {}).get("edges") or [] if isinstance(e, dict)),
    )
    raw = json.dumps({"nodes": nodes, "edges": edges},
                     ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _diag(level: str, code: str, message: str, **kw: Any) -> dict[str, Any]:
    d = {"level": level, "code": code, "message": message}
    d.update({k: v for k, v in kw.items() if v not in (None, "", [], {})})
    return d


def _fn_type_of(f: dict[str, Any], node_kind: str = "") -> str:
    """函数三型口径：fn_type 优先 → 节点 kind（action 节点是关键）→ fields.kind → 字段特征。

    旧 action 节点（如示例 act_notify_on_approve）fields 里没有 fn_type，靠节点 kind=action
    才能正确归为行动函数，否则会误判成派生。
    """
    t = _s(f.get("fn_type")).lower()
    if t in FN_TYPES:
        return t
    nk = _s(node_kind).lower()
    if nk == "action":
        return "action"
    if nk in ("function",):
        k = _STD_TO_TYPE.get(_s(f.get("kind")).lower(), "")
        if k:
            return k
    k = _STD_TO_TYPE.get(_s(f.get("kind")).lower(), "")
    if k:
        return k
    # 最后按字段特征：有 condition/effect 且无 logic → action；有 logic → derive
    if (_s(f.get("condition")) or _s(f.get("effect"))) and not _s(f.get("logic")):
        return "action"
    return "derive"


def _parse_attr_lines(text: Any) -> list[dict[str, Any]]:
    """属性定义行 ``名称:类型[:必填][:枚举或正则]`` → 结构化（与 validator 同口径，
    额外把第 4 段按前端同样的启发式拆成 enum 列表或 pattern）。"""
    attrs: list[dict[str, Any]] = []
    for line in _s(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(":")]
        name = parts[0] if parts else ""
        if not name:
            continue
        rng = parts[1] if len(parts) > 1 and parts[1] else "string"
        required = len(parts) > 2 and parts[2].lower() in ("1", "true", "required", "必填")
        extra = parts[3] if len(parts) > 3 else ""
        enum_vals: list[str] = []
        pattern = ""
        if extra:
            if (rng == "enum" or "," in extra or "，" in extra
                    or not re.search(r"[\\^$*+?()[\]|]", extra)):
                enum_vals = _split_list(extra)
                if rng not in BASE_RANGES or rng == "string":
                    rng = "enum" if enum_vals else rng
            else:
                pattern = extra
        attrs.append({"name": name, "range": rng, "required": required,
                      "enum": enum_vals, "pattern": pattern, "declared": True})
    return attrs


# ---------------------------------------------------------------- 指标口径识别
def infer_metric_algo(logic: Any, output: Any = "", name: Any = "") -> tuple[str, str]:
    """从函数 logic 文本里识别可确定的指标口径。

    返回 ``(algo, 依据)``；识别不出返回 ``("", 原因)``——**绝不默认 Sum**。
    顺序按「最具体 → 最笼统」，避免 ``count(distinct x)`` 被 ``count(*)`` 抢走。
    """
    src = _s(logic)
    low = src.lower()
    hay = f"{low} {_s(output).lower()} {_s(name).lower()}"

    if "去重拼接" in low or "strconcatdeduplicate" in low:
        return "StrConcatDeduplicate", "logic 含「去重拼接」"
    if "count(distinct" in low or "count( distinct" in low or "去重计数" in low \
            or "distinct count" in low or "独立数" in low or "不重复数" in low:
        return "DistinctCountHllp", "logic 含 count(distinct …) / 去重计数"
    if "非空率" in low or "fill_rate" in low or "填充率" in low:
        return "FillRate", "logic 含「非空率」"
    if "is not null" in low and "count(*)" in low:
        return "FillRate", "logic 形如 sum(case when x is not null…)/count(*)，按非空率"
    m = _GROUP_BY_RE.search(src)
    if m or "分布" in low or "占比" in low or "频次" in low or "频数" in low:
        return "FreqMap", ("logic 含 group by" + (f"（{m.group(1)}）" if m else "")
                           + "，按频次统计")
    if "product(" in low or "连乘" in low or "累积乘" in low:
        return "Product", "logic 含 product( / 连乘"
    if "avg(" in low or "average(" in low or "平均" in low or "均值" in low:
        return "Avg", "logic 含 avg( / 平均"
    if "sum(" in low or "求和" in low or "合计" in low or "汇总金额" in low:
        return "Sum", "logic 含 sum( / 求和"
    if "min(" in low or "最小" in low or "最低" in low:
        return "Min", "logic 含 min( / 最小"
    if "max(" in low or "最大" in low or "最高" in low:
        return "Max", "logic 含 max( / 最大"
    if "分位" in low or "quantile" in low or "percentile" in low:
        return "QuantileDoubles", "logic 含分位 / quantile"
    if "时间序列" in low or "time series" in low or "timeseries" in low:
        return "TimeSeries", "logic 含「时间序列」"
    if "时间戳集合" in low or "timestamp set" in low:
        return "TimestampSet", "logic 含「时间戳集合」"
    if "拼接" in low or "concat" in low or "串联" in low:
        return "StrConcat", "logic 含拼接 / concat"
    if "count(*)" in low or "实例数" in low or "记录数" in low or "条数" in low:
        return "RowCount", "logic 含 count(*) / 实例数"
    if "distinct" in low or "去重" in low:
        return "DistinctCountHllp", "logic 含 distinct / 去重"
    if "集合求交" in low or "intersect" in low:
        return "CollectionIntersect", "logic 含集合求交 / intersect"
    if "集合拼接" in low or "数组拼接" in low:
        return "CollectionConcat", "logic 含集合拼接"
    if _ORDER_BY_RE.search(src):
        return "OrderedLast", "logic 含 order by，按业务时间取最新"
    _ = hay  # 已并入 low，保留变量说明来源
    return "", "无法从 logic 识别出可确定的口径（请改用属性面板的「指标函数」下拉，或把 logic 写成 select 风格聚合）"


# ---------------------------------------------------------------- 函数三型规划
def _split_args(text: Any) -> list[str]:
    """拆输入清单：``a, b`` / ``['a','b']`` / ``a.b`` 都视为引用名。"""
    if isinstance(text, (list, tuple)):
        return [x for x in (_s(i) for i in text) if x]
    out: list[str] = []
    for part in _split_list(text):
        # a.b / t.col 保留点路径； ostatní 原样
        out.append(part.strip())
    return out


def _plan_metric(f: dict[str, Any], owner_attr: dict[str, Any] | None) -> dict[str, Any]:
    """指标函数规划：确定 algo（来源可追溯）+ 输入引用 + 可执行性。"""
    name = _s(f.get("name")) or _s(f.get("label")) or "未命名指标"
    label = _s(f.get("label")) or name
    logic = _s(f.get("logic"))
    output = _s(f.get("output"))
    inputs = _split_args(f.get("inputs"))

    algo, source, why = "", "", ""
    if owner_attr and _s(owner_attr.get("metric_fn")) in ALL_ALGOS:
        algo, source = _s(owner_attr.get("metric_fn")), "attribute.metric_fn"
    if not algo and _s(f.get("agg")) in ALL_ALGOS:
        algo, source = _s(f.get("agg")), "function.agg"
    if not algo:
        algo, why = infer_metric_algo(logic, output, name)
        if algo:
            source = "logic"

    executable = algo in ALL_ALGOS
    reason = "" if executable else (
        "未确定指标口径：" + (why or "属性指标函数 / 函数 agg / logic 均无可识别口径")
    )
    return {
        "id": _s(f.get("id")), "name": name, "label": label, "type": "metric",
        "owner_attr": _s(owner_attr.get("name")) if owner_attr else "",
        "algo": algo, "algo_label": algo_label(algo) if algo else "",
        "official": is_official(algo) if algo else False,
        "source": source, "inputs": inputs, "output": output,
        "executable": executable, "reason": reason, "logic": logic,
    }


def _plan_derive(f: dict[str, Any], owner_attr: dict[str, Any] | None) -> dict[str, Any]:
    """派生函数规划：只认三种可求值写法，自然语言判不可执行并给改写提示。"""
    name = _s(f.get("name")) or _s(f.get("label")) or "未命名派生"
    label = _s(f.get("label")) or name
    logic = _s(f.get("logic"))
    inputs = _split_args(f.get("inputs"))
    mode, target, expr_text = "", "", ""

    m = _ASSIGN_RE.match(logic)
    if m:
        mode, target, expr_text = "assign", m.group(1), m.group(2)
    else:
        m = _IF_RE.match(logic)
        if m:
            mode, target = "ifelse", _s(f.get("output"))
            expr_text = logic  # engine 端按三段处理
        elif logic and not _s(f.get("condition")):
            mode, target, expr_text = "expr", _s(f.get("output")), logic

    refs: list[str] = []
    syntax_error = ""
    # 中文自然语言（如「算一下大概的薪水」）恰是合法 Python 标识符，ast 不会报错，
    # 但它不是可求值表达式。规则：整个 expr 是一个裸 token 且不是已知属性/输入名 → 判不可执行。
    known = {owner["name"] for owner in [owner_attr] if owner} | set(inputs)
    bare = re.fullmatch(r"[\w\u4e00-\u9fa5]+", expr_text) if expr_text else None
    if bare and expr_text not in known:
        syntax_error = "logic 是自然语言描述或未知标识符，不是可求值表达式"
    if expr_text and not syntax_error:
        # ifelse 的每段分别校验；assign/expr 整体校验
        if mode == "ifelse" and m:
            segments = [m.group(1), m.group(2)] + ([m.group(3)] if m.group(3) else [])
        else:
            segments = [expr_text]
        for seg in segments:
            if not seg:
                continue
            try:
                refs.extend(EX.references(seg))
            except EX.ExprError as exc:
                syntax_error = str(exc)
                break
    refs = sorted(set(r for r in refs if r))

    executable = bool(mode) and not syntax_error
    if not executable:
        reason = syntax_error or (
            "派生 logic 不是可求值写法（需 out = 表达式 / if 条件 then 值 else 值 / 纯表达式）"
        )
    else:
        reason = ""
    return {
        "id": _s(f.get("id")), "name": name, "label": label, "type": "derive",
        "owner_attr": _s(owner_attr.get("name")) if owner_attr else "",
        "mode": mode, "target": target, "expr": expr_text, "refs": refs,
        "inputs": inputs, "output": _s(f.get("output")),
        "executable": executable, "reason": reason, "logic": logic,
    }


def _plan_action(f: dict[str, Any], owner_attr: dict[str, Any] | None) -> dict[str, Any]:
    """行动函数规划：condition 必须能当布尔表达式求值；effect 只声明不执行。"""
    name = _s(f.get("name")) or _s(f.get("label")) or "未命名行动"
    label = _s(f.get("label")) or name
    condition = _s(f.get("condition"))
    effect = _s(f.get("effect"))
    refs: list[str] = []
    syntax_error = ""
    if condition:
        try:
            refs = sorted(set(r for r in EX.references(condition) if r))
            EX.parse(condition)
        except EX.ExprError as exc:
            syntax_error = str(exc)
    executable = bool(condition) and not syntax_error
    if not executable:
        reason = syntax_error or "缺少可求值的触发条件 condition"
    else:
        reason = ""
    return {
        "id": _s(f.get("id")), "name": name, "label": label, "type": "action",
        "owner_attr": _s(owner_attr.get("name")) if owner_attr else "",
        "condition": condition, "effect": effect,
        "effect_mode": "declared",  # 运行时只判触发并留痕，不执行外部副作用
        "refs": refs, "inputs": _split_args(f.get("inputs")),
        "executable": executable, "reason": reason,
        "trigger": _s(f.get("trigger")),
    }


def _plan_constraint(n: dict[str, Any]) -> dict[str, Any]:
    """约束规划：解析约束类型节点或对象上的约束声明为可判定结构。"""
    f = n.get("fields") if isinstance(n.get("fields"), dict) else {}
    raw = _s(f.get("constraint") or f.get("rule") or f.get("logic") or n.get("constraint"))
    name = _s(f.get("name") or f.get("label")) or "未命名约束"
    kind, target, args = "", "", []
    m = _SPECIAL_RE.match(raw)
    if m:
        kind = m.group(1).lower()
        if kind == "notnull":
            kind = "not_null"
        args = _split_list(m.group(2))
        target = args[0] if args else ""
    else:
        m = _REFER_RE.match(raw)
        if m:
            kind, target, args = "referential", m.group(1), [m.group(1), m.group(2)]
    # 类型节点声明的 required / enum / pattern 也当约束
    if not kind and _truthy(f.get("required")):
        kind, target = "not_null", _s(f.get("name"))
    if not kind and f.get("enum"):
        kind, target, args = "in", _s(f.get("name")), _split_list(f.get("enum"))
    if not kind and f.get("pattern"):
        kind, target, args = "matches", _s(f.get("name")), [_s(f.get("pattern"))]

    known = kind in ("unique", "not_null", "matches", "in_range", "in", "referential")
    return {
        "id": _s(n.get("id")), "name": name, "kind": kind, "target": target,
        "args": args, "raw": raw,
        "executable": known and bool(target),
        "reason": "" if (known and target) else "约束类型不可识别或缺目标字段（支持 unique/not_null/matches/in_range/in/references）",
    }


def _compile_semantics(nodes: list[dict[str, Any]], objects: list[dict[str, Any]],
                       attrs: list[dict[str, Any]], diags: list[dict[str, Any]]) -> dict[str, Any]:
    """把建模产物上的「语义→物理字段」映射编译成 IR.semantics（问数唯一推导根）。

    产出：
      entity_types: {对象名: 物理 entity_type}（来自对象 fields.fact_entity_type）
      meanings: [{meaning, object, attribute, predicate, fact_value, attr_id, node_id}]
                —— 过滤类「中文语义 → 事实库过滤条件」映射（计数用）
      lookups:  [{meaning, object, attribute, predicate, attr_id, node_id}]
                —— 检索类「中文语义 → 返回属性值」映射（「部门负责人是谁」用），
                   判定=有 value_meaning + fact_predicate、但无 fact_value
    缺失映射字段的属性不进 meanings/lookups，并出 ATTR_SEMANTICS_INCOMPLETE 提示（照实）。
    """
    by_id = {_s(n.get("id")): n for n in nodes}

    def fields_of(n: dict[str, Any]) -> dict[str, Any]:
        f = n.get("fields")
        return f if isinstance(f, dict) else {}

    entity_types: dict[str, str] = {}
    entity_alias_map: dict[str, str] = {}
    for o in objects:
        node = by_id.get(o["id"], {})
        f = fields_of(node)
        et = _s(f.get(_SEM_OBJECT_KEY))
        if et:
            entity_types[o["name"]] = et
            # 对象名 / 显示名 / 别名都指向同一物理 entity_type（供「员工总数」类问数从本体推导）
            for alias in {o["name"], o.get("label", ""), *_split_list(f.get("aliases"))}:
                if alias:
                    entity_alias_map[alias] = et

    meanings: list[dict[str, Any]] = []   # 过滤类：语义 → 过滤条件（计数）
    lookups: list[dict[str, Any]] = []    # 检索类：语义 → 返回属性值（如「部门负责人是谁」）
    for a in attrs:
        node = by_id.get(a["id"], {})
        f = fields_of(node)
        pred = _s(f.get("fact_predicate"))
        val = _s(f.get("fact_value"))
        meaning = _s(f.get("value_meaning"))
        if not (pred or val or meaning):
            continue  # 该属性没有语义映射意图，不参与问数
        etype = entity_types.get(a.get("object", ""), "")
        # 检索类：有 value_meaning + fact_predicate，但没有 fact_value
        # → 问句命中语义后返回「事实里的属性值」（人名等），不是做过滤计数。
        # 这是「部门负责人是谁」这类实体检索问数的推导根，仍是本体声明、非硬编码。
        if meaning and pred and not val:
            lookups.append({
                "meaning": meaning, "object": a.get("object", ""),
                "attribute": a["name"], "attr_id": a["id"], "node_id": a["id"],
                "predicate": pred, "entity_type": etype,
                "source": f"node:{a['id']}.fields.value_meaning/fact_predicate",
            })
            continue
        # 过滤类：三项齐全，语义 → predicate=fact_value 过滤条件
        missing = [k for k, v in (("fact_predicate", pred), ("fact_value", val),
                                  ("value_meaning", meaning)) if not v]
        if missing:
            diags.append(_diag(WARN, "ATTR_SEMANTICS_INCOMPLETE",
                               f"属性「{a['name']}」的语义映射缺字段：{', '.join(missing)}，"
                               f"该语义不可用于问数推导", attr=a["name"], node_id=a["id"]))
            continue
        meanings.append({
            "meaning": meaning, "object": a.get("object", ""),
            "attribute": a["name"], "attr_id": a["id"], "node_id": a["id"],
            "predicate": pred, "fact_value": val,
            "entity_type": entity_types.get(a.get("object", ""), ""),
            "source": f"node:{a['id']}.fields.{','.join(_SEM_ATTR_KEYS)}",
        })
    return {
        "entity_types": entity_types,
        "entity_alias_map": entity_alias_map,
        "meanings": meanings,
        "lookups": lookups,
        "ready": (bool(meanings) or bool(lookups))
                 and all(m["entity_type"] for m in (meanings + lookups)),
    }


def compile_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """把建模草稿编译成可执行 IR + 体检结论（纯函数，零 I/O）。

    IR 结构::

        {hash, objects:[{id,name,label,attrs:[...],declared_attrs:[...],...}],
         attrs:[...], unassigned_attrs:[...], functions:[...], relations:[...],
         constraints:[...], diagnostics:[...], scope_map:{}, stats:{}}
    """
    nodes = _norm_nodes(spec or {})
    edges = _norm_edges(spec or {})
    by_id = {_s(n.get("id")): n for n in nodes}
    diags: list[dict[str, Any]] = []

    def fields_of(n: dict[str, Any]) -> dict[str, Any]:
        f = n.get("fields")
        return f if isinstance(f, dict) else {}

    def name_of(n: dict[str, Any]) -> str:
        f = fields_of(n)
        return _s(f.get("name")) or _s(f.get("label")) or _s(n.get("id"))

    # ---- 1 entity 族里区分「业务对象」与「属性」
    # 判属性：来自另一 entity 的入边 / 属性专属字段 / 名字出现在某对象 attributes 声明
    entity_nodes = [n for n in nodes if n.get("kind") == "entity"]
    entity_ids = {_s(n.get("id")) for n in entity_nodes}
    entity_names = {name_of(n) for n in entity_nodes}
    declared_names: set[str] = set()
    for n in entity_nodes:
        for a in _parse_attr_lines(fields_of(n).get("attributes")):
            declared_names.add(a["name"])
    incoming_entity: dict[str, str] = {}
    for e in edges:
        if e["from"] in entity_ids and e["to"] in entity_ids:
            incoming_entity[e["to"]] = e["from"]

    def looks_attr(n: dict[str, Any]) -> bool:
        f = fields_of(n)
        if any(_s(f.get(k)) for k in _ATTR_ONLY_KEYS):
            return True
        if _s(n.get("id")) in incoming_entity:
            return True
        return name_of(n) in declared_names

    object_nodes = [n for n in entity_nodes if not looks_attr(n)]
    attr_nodes = [n for n in entity_nodes if looks_attr(n)]
    if not object_nodes and attr_nodes:
        # 全是属性、没有对象：如实诊断，不硬造对象
        diags.append(_diag(WARN, "NO_OBJECT_NODE", "草稿里只有属性节点，没有业务对象节点"))
    for n in entity_nodes:
        if not name_of(n) or name_of(n) == _s(n.get("id")) and not fields_of(n).get("name"):
            diags.append(_diag(WARN, "ENTITY_NO_NAME", f"实体节点 {_s(n.get('id'))} 未填名称", node_id=_s(n.get("id"))))

    # ---- 2 对象表（含声明属性）
    objects: list[dict[str, Any]] = []
    obj_by_id: dict[str, dict[str, Any]] = {}
    obj_names: list[str] = []
    for n in object_nodes:
        f = fields_of(n)
        key = _s(f.get("primary_key")) or "id"
        declared = _parse_attr_lines(f.get("attributes"))
        if declared and key not in [a["name"] for a in declared]:
            diags.append(_diag(INFO, "PK_NOT_IN_ATTRS",
                               f"对象「{name_of(n)}」的主键 {key} 未出现在属性声明中",
                               object=name_of(n), primary_key=key))
        o = {
            "id": _s(n.get("id")), "name": name_of(n), "label": _s(f.get("label")) or name_of(n),
            "primary_key": key, "type_system": _s(f.get("type_system")),
            "source_object": _s(f.get("source_object")),
            "target_entity": _s(f.get("target_entity")), "declared_attrs": declared,
            "attrs": [],
        }
        if not declared:
            diags.append(_diag(INFO, "OBJECT_NO_ATTR",
                               f"对象「{o['name']}」未声明属性（attributes 为空）", object=o["name"]))
        objects.append(o)
        obj_by_id[o["id"]] = o
        obj_names.append(o["name"])

    # ---- 3 属性表 + 归属三档兜底
    attr_by_id: dict[str, dict[str, Any]] = {}
    attrs: list[dict[str, Any]] = []
    unassigned_attrs: list[dict[str, Any]] = []
    for n in attr_nodes:
        f = fields_of(n)
        aname = name_of(n)
        a = {
            "id": _s(n.get("id")), "name": aname, "label": _s(f.get("label")) or aname,
            "range": _s(f.get("range")) or "string", "required": _truthy(f.get("required")),
            "enum": _split_list(f.get("enum")), "pattern": _s(f.get("pattern")),
            "source_field": _s(f.get("source_field")), "is_pk": _truthy(f.get("is_pk")),
            "desc": _s(f.get("desc")), "metric_fn": _s(f.get("metric_fn")),
            "object": "",
        }
        if not aname or aname == a["id"] and not f.get("name"):
            diags.append(_diag(WARN, "ATTR_NO_NAME", f"属性节点 {a['id']} 未填名称", node_id=a["id"]))
        # 档1: object → attr 入边
        owner_id = incoming_entity.get(a["id"], "")
        owner = obj_by_id.get(owner_id)
        if owner:
            a["object"] = owner["name"]
        # 档2: 字段指定
        if not a["object"]:
            want = _s(f.get("object") or f.get("parent") or f.get("owner_object"))
            owner = next((o for o in objects if o["name"] == want or o["id"] == want), None)
            if owner:
                a["object"] = owner["name"]
        # 档3: 唯一对象推断（仅当只有一个对象）
        if not a["object"] and len(objects) == 1:
            a["object"] = objects[0]["name"]
            diags.append(_diag(INFO, "ATTR_OWNER_INFERRED",
                               f"属性「{a['name']}」归属按唯一对象推断为「{a['object']}」", attr=a["name"]))
        if a["object"]:
            target = next((o for o in objects if o["name"] == a["object"]), None)
            if target is not None:
                target["attrs"].append(a)
        else:
            unassigned_attrs.append(a)
            diags.append(_diag(WARN, "ATTR_NO_OBJECT",
                               f"属性「{a['name']}」无法判定归属对象（不默认塞给第一个对象）", attr=a["name"]))
        attr_by_id[a["id"]] = a
        attrs.append(a)

    # ---- 4 函数三型（含旧 action 节点归一）
    func_incoming_attr: dict[str, str] = {}
    for e in edges:
        if e["from"] in attr_by_id:
            func_incoming_attr[e["to"]] = e["from"]
    functions: list[dict[str, Any]] = []
    for n in nodes:
        kind = n.get("kind")
        f = fields_of(n)
        if kind != "function" and not any(k in f for k in ("fn_type", "logic", "condition", "effect", "agg")):
            continue
        if kind not in ("function", "action"):
            continue
        ft = _fn_type_of(f, kind)
        # 归属：attr→func 边 → owner_attr 字段 → inputs 名等于属性名
        owner: dict[str, Any] | None = None
        fid = _s(n.get("id"))
        owner_id = func_incoming_attr.get(fid, "")
        if owner_id in attr_by_id:
            owner = attr_by_id[owner_id]
        if owner is None and _s(f.get("owner_attr")):
            want = _s(f.get("owner_attr"))
            owner = attr_by_id.get(want) or next((a for a in attrs if a["name"] == want), None)
        if owner is None:
            for inp in _split_args(f.get("inputs")):
                cand = next((a for a in attrs if a["name"] == inp), None)
                if cand:
                    owner = cand
                    break
        if ft == "metric":
            plan = _plan_metric(f, owner)
        elif ft == "derive":
            plan = _plan_derive(f, owner)
        else:
            plan = _plan_action(f, owner)
        plan["owner_attr"] = plan.get("owner_attr") or (owner["name"] if owner else "")
        if owner is None:
            diags.append(_diag(WARN, "FUNC_NO_OWNER",
                               f"函数「{plan['name']}」未挂到任何属性（孤儿函数）", function=plan["name"]))
        functions.append(plan)

    # ---- 5 关系 / 约束
    relations: list[dict[str, Any]] = []
    for n in nodes:
        if n.get("kind") != "relation":
            continue
        f = fields_of(n)
        relations.append({
            "id": _s(n.get("id")), "name": name_of(n),
            "source": _s(f.get("source_entity") or f.get("from")),
            "target": _s(f.get("target_entity") or f.get("to")),
            "cardinality": _s(f.get("cardinality")),
        })
    constraints: list[dict[str, Any]] = []
    for n in nodes:
        f = fields_of(n)
        is_cnode = n.get("kind") == "constraint"
        has_rule = any(_s(f.get(k)) for k in ("constraint", "rule", "logic"))
        if is_cnode or (n.get("kind") == "entity" and has_rule and not looks_attr(n)):
            constraints.append(_plan_constraint(n))

    # ---- 6 体检结论
    # 属性总数按名字去重合并：独立 attr 节点 ∪ 对象 attributes 声明的属性
    declared_attr_names: set[str] = set()
    for o in objects:
        for a in (o.get("declared_attrs") or []):
            if a.get("name"):
                declared_attr_names.add(a["name"])
    all_attr_names = {a["name"] for a in attrs} | declared_attr_names
    scope_map = {
        "objects": [o["name"] for o in objects],
        "attrs": sorted(all_attr_names),
        "functions": [f["name"] for f in functions],
    }
    ir = {
        "hash": spec_hash(spec or {}),
        "objects": objects, "attrs": attrs, "unassigned_attrs": unassigned_attrs,
        "functions": functions, "relations": relations, "constraints": constraints,
        "diagnostics": diags, "scope_map": scope_map,
        "stats": {
            "objects": len(objects), "attrs": len(all_attr_names),
            "attr_nodes": len(attrs), "declared_attrs": len(declared_attr_names),
            "functions": len(functions),
            "executable_functions": sum(1 for f in functions if f.get("executable")),
            "relations": len(relations), "constraints": len(constraints),
            "unassigned_attrs": len(unassigned_attrs),
        },
    }
    # ---- 5 语义映射（问数推导根，来自建模产物，不是代码硬编码）----
    ir["semantics"] = _compile_semantics(nodes, objects, attrs, diags)
    ir["health"] = diagnose(ir)
    return ir


def diagnose(ir: dict[str, Any]) -> dict[str, Any]:
    """从 IR 生成对外体检结论：能算什么 / 缺什么 / 整体可执行性（照实，不编造）。"""
    stats = ir.get("stats") or {}
    funcs = ir.get("functions") or []
    metrics = [f for f in funcs if f.get("type") == "metric" and f.get("executable")]
    derives = [f for f in funcs if f.get("type") == "derive" and f.get("executable")]
    actions = [f for f in funcs if f.get("type") == "action" and f.get("executable")]
    broken = [
        {"name": f.get("name", ""), "type": f.get("type", ""), "reason": f.get("reason", "")}
        for f in funcs if not f.get("executable")
    ]
    semantics = ir.get("semantics") or {}
    meanings = semantics.get("meanings") or []
    lookups = semantics.get("lookups") or []
    return {
        "metric_algorithms": sorted({f["algo"] for f in metrics if f.get("algo")}),
        "metrics": [{"name": f["name"], "algo": f.get("algo"), "owner": f.get("owner_attr", "")} for f in metrics],
        "semantics": {
            "queryable_meanings": [m["meaning"] for m in meanings],
            "queryable_lookups": [m["meaning"] for m in lookups],
            "ready": bool(semantics.get("ready")),
            "entity_types": semantics.get("entity_types", {}),
        },
        "derives": [f["name"] for f in derives],
        "actions": [f["name"] for f in actions],
        "broken_functions": broken,
        "objects": [o["name"] for o in (ir.get("objects") or [])],
        "attr_count": stats.get("attrs", 0),
        "executable_count": stats.get("executable_functions", 0),
        "function_count": stats.get("functions", 0),
        "ready": bool(stats.get("objects")) and not broken and not stats.get("unassigned_attrs"),
    }


def self_check() -> dict[str, Any]:
    """纯函数自检：对象/属性识别、归属兜底、三型分派、体检结论。"""
    spec = {
        "nodes": [
            {"id": "o1", "kind": "entity", "fields": {"name": "员工", "primary_key": "工号", "attributes": "工号:string\n月薪:number"}},
            {"id": "a1", "kind": "entity", "fields": {"name": "月薪", "range": "number", "metric_fn": "Sum"}},
            {"id": "a2", "kind": "entity", "fields": {"name": "工号", "range": "string"}},
            {"id": "fn1", "kind": "function", "fields": {"name": "avg_salary", "fn_type": "metric", "inputs": ["月薪"], "agg": "Avg", "logic": "avg(月薪)"}},
            {"id": "fn2", "kind": "function", "fields": {"name": "double", "fn_type": "derive", "logic": "result = 月薪 * 2"}},
            {"id": "fn3", "kind": "function", "fields": {"name": "warn", "fn_type": "action", "condition": "月薪 > 100000", "effect": "标记高薪"}},
            {"id": "fn4", "kind": "function", "fields": {"name": "vague", "fn_type": "derive", "logic": "算一下大概的薪水"}},
        ],
        "edges": [
            {"from": "o1", "to": "a1"}, {"from": "o1", "to": "a2"},
            {"from": "a1", "to": "fn1"}, {"from": "a1", "to": "fn2"}, {"from": "a1", "to": "fn3"},
        ],
    }
    ir = compile_spec(spec)
    h = ir["health"]
    ok = (
        ir["stats"]["objects"] == 1
        and ir["stats"]["attrs"] == 2
        and ir["stats"]["functions"] == 4
        and len(h["metric_algorithms"]) >= 1
        and len(h["broken_functions"]) == 1
        and h["broken_functions"][0]["name"] == "vague"
        and all(a["object"] == "员工" for a in ir["attrs"])
        and ir["hash"] == compile_spec(spec)["hash"]  # 幂等
    )
    # 空草稿不崩
    empty = compile_spec({})
    ok = ok and empty["stats"]["objects"] == 0 and empty["health"]["ready"] is False
    return {"pass": bool(ok), "stats": ir["stats"], "health": h}
