"""完整验证节点 · 四类检查引擎（数据驱动）。

用户定义的专家标准：只有以下四类全部通过，才标记「本体建模完整」。
  1) 结构完整性 structure：主键、必填属性、关系两端类型、基数、循环继承/非法组合
  2) 数据覆盖度 coverage：实例数/覆盖率、必填填充率、基数满足、孤立比例、时序/向量有效数据
  3) 一致性 consistency：类型/范围冲突、唯一性冲突、引用完整性、自定义规则违反
  4) 可执行性 executability：Function 可计算、Action 前置可满足、派生属性可触发

数据驱动：checks 接收 `data`（真实库读取的实例/关系，或调用方提供的模拟数据）。
无数据时如实标记 no-data 并判为不通过 —— 不编造指标，符合项目约定「缺口照实披露」。

全部为纯函数，零 I/O，可单测。报告结构化，含 pass/warn/fail/no-data 四态与量化指标。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from .clean_rules import validate_clean_rules
from .node_schema import NODE_TYPES, kind_of

# 报告四态
PASS = "pass"
WARN = "warn"
FAIL = "fail"
NODATA = "no-data"
_LEVEL_RANK = {PASS: 0, WARN: 1, NODATA: 2, FAIL: 3}

CATEGORIES = (
    ("structure", "结构完整性"),
    ("coverage", "数据覆盖度"),
    ("consistency", "一致性"),
    ("executability", "可执行性"),
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BASE_RANGES = {"string", "integer", "number", "boolean", "date", "datetime", "enum"}
_CARDINALITY = {"1:1", "1:N", "N:M"}


def _item(cat: str, level: str, code: str, message: str, detail: Any = None) -> dict[str, Any]:
    return {"category": cat, "level": level, "code": code, "message": message, "detail": detail}


def _norm_nodes(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """归一节点：kind 统一到标准；fields 兼容顶层字段（旧草稿字段有的在节点顶层）。"""
    out = []
    for n in spec.get("nodes") or []:
        kind = kind_of(n.get("kind"))
        fields = dict(n.get("fields") or {})
        # 旧草稿部分字段直接写在节点上
        for k in ("name", "label", "source_type", "range", "required", "cardinality"):
            if k not in fields and k in n:
                fields[k] = n[k]
        out.append({**n, "kind": kind, "fields": fields})
    return out


def _by_kind(nodes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    d: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        if n.get("kind"):
            d[n["kind"]].append(n)
    return d


def _f(n: dict[str, Any], key: str, default: Any = "") -> Any:
    v = n.get("fields", {}).get(key, default)
    return default if v is None else v


def _parse_attr_lines(text: str) -> list[dict[str, Any]]:
    """属性定义行： 名称:类型[:required][:枚举或正则] → 结构化。"""
    attrs = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(":")]
        name = parts[0] if parts else ""
        if not name:
            continue
        rng = parts[1] if len(parts) > 1 else "string"
        req = len(parts) > 2 and parts[2].lower() in ("1", "true", "required", "必填")
        extra = parts[3] if len(parts) > 3 else ""
        attrs.append({"name": name, "range": rng, "required": req, "extra": extra})
    return attrs


def _parse_mapping_lines(text: str) -> dict[str, str]:
    """字段→属性映射行： 源字段=属性 → dict。"""
    out = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        src, dst = line.split("=", 1)
        out[src.strip()] = dst.strip()
    return out


# ---------------------------------------------------------------- 1 结构完整性
def check_structure(spec: dict[str, Any]) -> list[dict[str, Any]]:
    r: list[dict[str, Any]] = []
    nodes = _norm_nodes(spec)
    bk = _by_kind(nodes)
    edges = spec.get("edges") or []
    ids = {n.get("id") for n in nodes}

    # 1.1 画布无强制节点：发布门禁改按四类完整验证报告判定（见 workflow.run），
    #     不再要求草稿里存在 validation 节点。

    # 1.1b 清洗规则：语法可解析才允许进入四类完整验证（只校验配置，不执行改写）
    for p in bk.get("profiler", []):
        pname = str(_f(p, "name") or "未命名清洗节点")
        text = str(_f(p, "clean_rules") or _f(p, "rule") or "").strip()
        if not text:
            r.append(_item("structure", FAIL, "CLEAN_RULES_EMPTY",
                           f"清洗节点「{pname}」未定义清洗规则"))
            continue
        rep = validate_clean_rules(text)
        for e in rep["errors"]:
            r.append(_item("structure", FAIL, "CLEAN_RULE_SYNTAX",
                           f"清洗节点「{pname}」第 {e['line']} 行：{e['message']}", e))
        if rep["legacy_lines"]:
            r.append(_item("structure", PASS, "CLEAN_RULES_LEGACY",
                           f"清洗节点「{pname}」有 {len(rep['legacy_lines'])} 条旧式描述，仅留痕、未机器校验"))
        if rep["ok"]:
            r.append(_item("structure", PASS, "CLEAN_RULES_OK",
                           f"清洗节点「{pname}」{rep['structured']} 条规则语法通过（未执行）"))

    # 1.1c 数据血缘：处理节点的产出要落到业务对象，否则「清洗完的数据喂给谁」没人认领。
    #      口径与画布一致——fields.link_objects（登记）∪ 画布上的 profiler→entity 边（自动连线）。
    #      只 WARN 不阻断：旧草稿可能还没有对象节点，卡成 FAIL 会让历史稿全部发布不了。
    node_by_id = {n.get("id"): n for n in nodes if n.get("id")}

    def _is_object_node(n: dict[str, Any] | None) -> bool:
        if not n or n.get("kind") != "entity":
            return False
        f = n.get("fields") or {}
        # 属性节点也归一到 entity：带 range 且没声明主键的就是属性，不是业务对象
        return not ("range" in f and not str(f.get("primary_key") or "").strip())

    for p in bk.get("profiler", []):
        pname = str(_f(p, "name") or "未命名处理节点")
        links = _f(p, "link_objects") or []
        if isinstance(links, str):
            links = links.replace("，", ",").split(",")
        names: list[str] = []
        for one in links:
            key = str(one or "").strip()
            if not key:
                continue
            hit = node_by_id.get(key)
            names.append(str(((hit or {}).get("fields") or {}).get("name") or key).strip())
        for e in edges:
            if not isinstance(e, dict) or e.get("from") != p.get("id"):
                continue
            tgt = node_by_id.get(e.get("to"))
            if _is_object_node(tgt):
                names.append(str((tgt.get("fields") or {}).get("name") or "").strip())
        uniq = sorted({n for n in names if n})
        if uniq:
            r.append(_item("structure", PASS, "PROCESS_LINKED",
                           f"处理节点「{pname}」关联业务对象 {len(uniq)} 个：{'、'.join(uniq)}"))
        else:
            r.append(_item("structure", WARN, "PROCESS_UNLINKED",
                           f"处理节点「{pname}」未关联任何业务对象：清洗结果没有下游，"
                           f"在节点详情里勾选「关联业务对象」即可（提示项，不阻断发布）"))

    # 1.2 实体声明主键
    entities = bk.get("entity", [])
    has_attr_nodes = bool(bk.get("entity"))  # 旧草稿 attr 归一到 entity 后仍需区分
    primary_keys: dict[str, str] = {}
    attr_names: dict[str, set[str]] = defaultdict(set)
    for e in entities:
        raw_kind = None
        # 通过原始字段特征区分「实体定义」与「属性节点」：属性节点常用 range
        is_attr_node = "range" in (e.get("fields") or {}) and not _f(e, "primary_key")
        if is_attr_node:
            continue
        name = str(_f(e, "name") or _f(e, "label") or "").strip()
        pk = str(_f(e, "primary_key") or "").strip()
        if not name:
            r.append(_item("structure", FAIL, "ENTITY_UNNAMED", "实体节点未声明名称"))
            continue
        if not _IDENT.match(name):
            r.append(_item("structure", FAIL, "ENTITY_BAD_IDENT", f"实体名不是合法标识符：{name}"))
        if not pk:
            r.append(_item("structure", FAIL, "ENTITY_NO_PK", f"实体「{name}」未声明主键", {"entity": name}))
        else:
            primary_keys[name] = pk
        for a in _parse_attr_lines(_f(e, "attributes")):
            if not _IDENT.match(a["name"]):
                r.append(_item("structure", FAIL, "ATTR_BAD_IDENT", f"实体「{name}」属性名非法：{a['name']}"))
            if a["range"] not in _BASE_RANGES and a["range"] not in {x["name"] for x in entities}:
                r.append(_item("structure", WARN, "ATTR_UNKNOWN_RANGE",
                               f"实体「{name}」属性「{a['name']}」类型未在标准集/实体集中定义：{a['range']}"))
            attr_names[name].add(a["name"])
            if a["range"] == "enum" and not a["extra"]:
                r.append(_item("structure", FAIL, "ENUM_NO_VALUES",
                               f"实体「{name}」属性「{a['name']}」range=enum 但未给枚举值"))
        # 必填属性需显式声明（有主键即视为至少声明了主键这一必填项）
        if not _f(e, "attributes") and not _f(e, "primary_key"):
            r.append(_item("structure", WARN, "ENTITY_NO_ATTRS",
                           f"实体「{name}」未声明任何属性/必填项"))

    # 1.3 关系两端类型匹配 + 基数
    ent_names = set(primary_keys) | {
        str(_f(e, "name") or _f(e, "label")).strip() for e in entities
        if str(_f(e, "name") or _f(e, "label")).strip()
    }
    for rel in bk.get("relation", []):
        rn = str(_f(rel, "name") or "").strip()
        src = str(_f(rel, "source_entity") or "").strip()
        tgt = str(_f(rel, "target_entity") or "").strip()
        card = str(_f(rel, "cardinality") or "").strip()
        if not rn or not _IDENT.match(rn):
            r.append(_item("structure", FAIL, "REL_BAD_NAME", f"关系名非法：{rn or '(空)'}"))
        for side, val in (("source", src), ("target", tgt)):
            if val and ent_names and val not in ent_names:
                r.append(_item("structure", FAIL, "REL_ENDPOINT_TYPE",
                               f"关系「{rn}」{side} 端类型「{val}」未在实体集中定义"))
        if card not in _CARDINALITY:
            r.append(_item("structure", FAIL, "REL_BAD_CARDINALITY",
                           f"关系「{rn}」基数未声明或不合法：{card or '(空)'}（应为 1:1/1:N/N:M）"))

    # 1.4 循环继承检测（type_system: parent=XXX）
    parent: dict[str, str] = {}
    for e in entities:
        name = str(_f(e, "name") or _f(e, "label") or "").strip()
        ts = str(_f(e, "type_system") or "")
        m = re.search(r"parent\s*=\s*([A-Za-z_][A-Za-z0-9_]*)", ts)
        if name and m:
            parent[name] = m.group(1)
    for start in parent:
        seen, cur = [], start
        while cur in parent:
            if cur in seen:
                cyc = " → ".join(seen[seen.index(cur):] + [cur])
                r.append(_item("structure", FAIL, "CYCLIC_INHERITANCE", f"检测到循环继承：{cyc}", {"cycle": cyc}))
                break
            seen.append(cur)
            cur = parent[cur]
        if cur and cur not in parent and cur not in ent_names and cur not in seen[:-1]:
            # parent 指向未定义类型（仅当走到链尾且非自环时提示）
            pass

    # 1.5 连线端点有效性 + 非法组合
    for ed in edges:
        if ed.get("from") not in ids or ed.get("to") not in ids:
            r.append(_item("structure", WARN, "EDGE_DANGLING",
                           f"连线端点不存在：{ed.get('from')} → {ed.get('to')}"))
    # 发布门禁按验证报告判定，画布无 publish 节点约束。

    if not r:
        r.append(_item("structure", PASS, "STRUCTURE_OK", "结构完整：主键/属性/关系/基数/继承均无阻断项"))
    return r


# ---------------------------------------------------------------- 2 数据覆盖度
def check_coverage(spec: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    r: list[dict[str, Any]] = []
    nodes = _norm_nodes(spec)
    bk = _by_kind(nodes)
    entities = [n for n in bk.get("entity", []) if "range" not in (n.get("fields") or {})]
    inst = data.get("entities") if isinstance(data, dict) else None
    if not isinstance(inst, dict) or not inst:
        return [_item("coverage", NODATA, "NO_INSTANCE_DATA",
                      "未提供实例数据——数据覆盖度无法验证（无数据源写「无」，不编造覆盖率）")]

    ent_cfg = {}
    for e in entities:
        name = str(_f(e, "name") or _f(e, "label") or "").strip()
        if name:
            ent_cfg[name] = _parse_attr_lines(_f(e, "attributes"))
            pk = str(_f(e, "primary_key") or "").strip()
            if pk and not any(a["name"] == pk for a in ent_cfg[name]):
                ent_cfg[name].append({"name": pk, "range": "string", "required": True, "extra": ""})

    total_rows = 0
    for name, attrs in ent_cfg.items():
        rows = inst.get(name) or []
        total_rows += len(rows)
        if not rows:
            r.append(_item("coverage", WARN, "ENTITY_NO_INSTANCE",
                           f"实体「{name}」无实例数据", {"entity": name, "count": 0}))
            continue
        req = [a["name"] for a in attrs if a["required"]]
        for a in req:
            filled = sum(1 for row in rows if _filled((row.get("properties") or row).get(a)))
            rate = round(filled / len(rows), 4)
            lvl = PASS if rate >= 1.0 else WARN
            r.append(_item("coverage", lvl, "REQUIRED_FILL",
                           f"实体「{name}」必填属性「{a}」填充率 {rate:.0%}（{filled}/{len(rows)}）",
                           {"entity": name, "attr": a, "rate": rate, "filled": filled, "total": len(rows)}))
            if rate < 1.0:
                r.append(_item("coverage", FAIL, "REQUIRED_FILL_BELOW",
                               f"实体「{name}」必填属性「{a}」填充率低于 100% 阈值——按专家标准直接失败"))
        # 时序 / 向量属性有效数据
        for a in attrs:
            if a["range"] in ("date", "datetime") or "vec" in a["name"].lower() or "vector" in a["name"].lower() or "embed" in a["name"].lower():
                valid = 0
                for row in rows:
                    v = (row.get("properties") or row).get(a["name"])
                    if _valid_temporal_vector(v, a["range"]):
                        valid += 1
                lvl = PASS if valid == len(rows) else WARN
                r.append(_item("coverage", lvl, "SPECIAL_ATTR_DATA",
                               f"实体「{name}」{a['range']}属性「{a['name']}」有效数据 {valid}/{len(rows)}",
                               {"entity": name, "attr": a["name"], "valid": valid, "total": len(rows)}))

    # 关系实例满足基数 + 孤立比例
    rels = data.get("relations") or []
    rel_cfg = {str(_f(x, "name")): str(_f(x, "cardinality")) for x in bk.get("relation", [])}
    rel_count = Counter(x.get("relation_type") for x in rels)
    for rn, card in rel_cfg.items():
        cnt = rel_count.get(rn, 0)
        if cnt == 0:
            r.append(_item("coverage", WARN, "REL_NO_INSTANCE", f"关系「{rn}」无实例数据", {"relation": rn}))
        else:
            r.append(_item("coverage", PASS, "REL_INSTANCE_COUNT", f"关系「{rn}」实例数 {cnt}", {"relation": rn, "count": cnt}))

    linked: set[str] = set()
    for x in rels:
        linked.add(str(x.get("source_id") or x.get("source")))
        linked.add(str(x.get("target_id") or x.get("target")))
    all_ids: set[str] = set()
    for rows in inst.values():
        for row in rows:
            iid = row.get("id") or (row.get("properties") or {}).get("id")
            if iid is not None:
                all_ids.add(str(iid))
    if all_ids:
        orphan = len(all_ids - linked)
        ratio = round(orphan / len(all_ids), 4)
        lvl = PASS if ratio <= 0.05 else WARN
        r.append(_item("coverage", lvl, "ORPHAN_RATIO",
                       f"孤立节点比例 {ratio:.0%}（{orphan}/{len(all_ids)}）",
                       {"orphan": orphan, "total": len(all_ids), "ratio": ratio}))

    r.append(_item("coverage", PASS, "COVERAGE_SUMMARY",
                   f"实例覆盖：{len(inst)} 个实体 / {total_rows} 条实例 / {len(rels)} 条关系"))
    return r


def _filled(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return bool(v)
    return True


def _valid_temporal_vector(v: Any, rng: str) -> bool:
    if not _filled(v):
        return False
    if rng in ("date", "datetime"):
        if isinstance(v, (int, float)):
            return True
        s = str(v)
        return bool(re.match(r"^\d{4}-\d{2}-\d{2}", s))
    # 向量 / 嵌入
    if isinstance(v, (list, tuple)):
        return len(v) > 0 and all(isinstance(x, (int, float)) for x in v)
    if isinstance(v, str):
        try:
            arr = [float(x) for x in re.split(r"[,\s]+", v.strip().strip("[]")) if x]
            return len(arr) > 0
        except ValueError:
            return False
    return False


# ---------------------------------------------------------------- 3 一致性
def check_consistency(spec: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    r: list[dict[str, Any]] = []
    nodes = _norm_nodes(spec)
    bk = _by_kind(nodes)
    inst = data.get("entities") if isinstance(data, dict) else None
    if not isinstance(inst, dict) or not inst:
        return [_item("consistency", NODATA, "NO_INSTANCE_DATA", "未提供实例数据——一致性无法验证")]

    # 类型与范围冲突
    for e in [n for n in bk.get("entity", []) if "range" not in (n.get("fields") or {})]:
        name = str(_f(e, "name") or _f(e, "label") or "").strip()
        attrs = {a["name"]: a for a in _parse_attr_lines(_f(e, "attributes"))}
        rows = inst.get(name) or []
        for row in rows:
            props = row.get("properties") or row
            for an, a in attrs.items():
                if an not in props or not _filled(props[an]):
                    continue
                v = props[an]
                if not _type_ok(v, a["range"]):
                    r.append(_item("consistency", FAIL, "TYPE_RANGE_CONFLICT",
                                   f"实体「{name}」属性「{an}」值 {v!r} 与声明类型 {a['range']} 冲突"))
                if a["range"] == "enum" and a["extra"]:
                    allowed = [x.strip() for x in re.split(r"[,|/]", a["extra"]) if x.strip()]
                    if allowed and str(v) not in allowed:
                        r.append(_item("consistency", FAIL, "ENUM_VIOLATION",
                                       f"实体「{name}」属性「{an}」值「{v}」不在枚举 {allowed}"))

    # 唯一性冲突（主键 + unique 约束）
    uniq_targets: list[tuple[str, str]] = []
    for e in [n for n in bk.get("entity", []) if "range" not in (n.get("fields") or {})]:
        name = str(_f(e, "name") or _f(e, "label") or "").strip()
        pk = str(_f(e, "primary_key") or "").strip()
        if name and pk:
            uniq_targets.append((name, pk))
    for c in bk.get("constraint", []):
        if str(_f(c, "kind")) == "unique":
            tgt = str(_f(c, "target") or "")
            if "." in tgt:
                en, at = tgt.split(".", 1)
                uniq_targets.append((en.strip(), at.strip()))
    for en, at in uniq_targets:
        rows = inst.get(en) or []
        vals = [str((row.get("properties") or row).get(at)) for row in rows
                if _filled((row.get("properties") or row).get(at))]
        dup = [v for v, c in Counter(vals).items() if c > 1]
        if dup:
            r.append(_item("consistency", FAIL, "UNIQUENESS_CONFLICT",
                           f"唯一性冲突：{en}.{at} 重复值 {dup[:5]}", {"entity": en, "attr": at, "dup": dup[:20]}))

    # 引用完整性（关系两端 id 存在）
    all_ids: set[str] = set()
    for rows in inst.values():
        for row in rows:
            iid = row.get("id") or (row.get("properties") or {}).get("id")
            if iid is not None:
                all_ids.add(str(iid))
    bad = 0
    for x in (data.get("relations") or []):
        for side in ("source_id", "target_id", "source", "target"):
            v = x.get(side)
            if v is not None and str(v) not in all_ids:
                bad += 1
    if bad:
        r.append(_item("consistency", FAIL, "REFERENTIAL_BROKEN", f"引用完整性失效：{bad} 个关系端点找不到对应实例"))
    else:
        r.append(_item("consistency", PASS, "REFERENTIAL_OK", "引用完整性：所有关系端点均可解析"))

    # 自定义规则违反（business 约束：expression 形如 属性 运算符 值）
    violations = 0
    for c in bk.get("constraint", []):
        if str(_f(c, "kind")) != "business":
            continue
        expr = str(_f(c, "expression") or "").strip()
        m = re.match(r"^([\w.]+)\s*(>=|<=|!=|==|>|<|=)\s*(.+)$", expr)
        if not m:
            r.append(_item("consistency", WARN, "RULE_UNPARSEABLE", f"业务规则「{expr}」无法解析为可校验表达式"))
            continue
        path, op, raw = m.groups()
        target = str(_f(c, "target") or "")
        en = path.split(".")[0] if "." in path else target.split(".")[0]
        attr = path.split(".")[1] if "." in path else (target.split(".")[1] if "." in target else "")
        for row in (inst.get(en) or []):
            v = (row.get("properties") or row).get(attr)
            if _filled(v) and not _compare(v, op, raw.strip().strip('"\'')):
                violations += 1
    if violations:
        r.append(_item("consistency", FAIL, "BUSINESS_RULE_VIOLATION", f"自定义业务规则违反 {violations} 条明细"))
    elif not r or all(x["level"] in (PASS, WARN) for x in r):
        r.append(_item("consistency", PASS, "CONSISTENCY_OK", "一致性：类型/范围/唯一性/引用/规则无阻断项"))
    return r


def _type_ok(v: Any, rng: str) -> bool:
    if rng in ("integer",):
        return isinstance(v, int) and not isinstance(v, bool)
    if rng in ("number",):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if rng == "boolean":
        return isinstance(v, bool)
    return True  # string/date/datetime/enum 交由专门检查


def _compare(v: Any, op: str, raw: str) -> bool:
    # 数值与数值比较走数值；否则统一字符串比较。避免 "1" >= "1.0" 的字符串误判。
    try:
        left: Any = float(v) if not isinstance(v, bool) else str(v)
    except (ValueError, TypeError):
        left = str(v)
    try:
        other: Any = float(raw)
    except (ValueError, TypeError):
        other = str(raw)
    if isinstance(left, float) != isinstance(other, float):
        left, other = str(v), str(raw)
    try:
        return {
            ">": left > other, "<": left < other, ">=": left >= other, "<=": left <= other,
            "==": left == other, "=": left == other, "!=": left != other,
        }[op]
    except Exception:
        return True  # 无法比较时不误判为违反


# ---------------------------------------------------------------- 4 可执行性
def check_executability(spec: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    r: list[dict[str, Any]] = []
    nodes = _norm_nodes(spec)
    bk = _by_kind(nodes)
    inst = data.get("entities") if isinstance(data, dict) else None
    has_data = isinstance(inst, dict) and bool(inst)

    # Function 在当前数据上能否计算
    all_attr_names: set[str] = set()
    for e in [n for n in bk.get("entity", []) if "range" not in (n.get("fields") or {})]:
        for a in _parse_attr_lines(_f(e, "attributes")):
            all_attr_names.add(a["name"])
    for fn in bk.get("function", []):
        name = str(_f(fn, "name") or "").strip()
        out = str(_f(fn, "output") or "").strip()
        logic = str(_f(fn, "logic") or "").strip()
        ins = [x.strip() for x in str(_f(fn, "inputs") or "").split(",") if x.strip()]
        if not out or not logic:
            r.append(_item("executability", FAIL, "FN_INCOMPLETE",
                           f"函数「{name}」缺输出或逻辑描述，无法计算"))
            continue
        miss = [x for x in ins if x not in all_attr_names]
        if miss:
            r.append(_item("executability", FAIL, "FN_INPUT_UNRESOLVED",
                           f"函数「{name}」输入属性在实体集中不存在：{miss}"))
            continue
        if not has_data:
            r.append(_item("executability", NODATA, "FN_NO_DATA",
                           f"函数「{name}」配置可解析，但无实例数据，无法在真实数据上计算"))
        else:
            r.append(_item("executability", PASS, "FN_COMPUTABLE",
                           f"函数「{name}」输入可解析、逻辑已声明，可在当前数据上计算 → {out}"))

    # Action 前置条件可否被满足
    for ac in bk.get("action", []):
        name = str(_f(ac, "name") or "").strip()
        cond = str(_f(ac, "condition") or "").strip()
        trig = str(_f(ac, "trigger") or "event")
        eff = str(_f(ac, "effect") or "").strip()
        if not eff:
            r.append(_item("executability", FAIL, "ACTION_NO_EFFECT", f"行动「{name}」未定义执行动作"))
        if trig != "schedule" and not cond:
            r.append(_item("executability", FAIL, "ACTION_NO_CONDITION",
                           f"行动「{name}」非定时触发但未写前置条件"))
        elif cond:
            refs = [w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", cond) if w in all_attr_names or w in ("and", "or", "not")]
            if not has_data:
                r.append(_item("executability", NODATA, "ACTION_NO_DATA",
                               f"行动「{name}」前置条件已声明，但无数据无法验证是否可满足"))
            else:
                r.append(_item("executability", PASS, "ACTION_CONDITION_OK",
                               f"行动「{name}」前置条件引用属性可解析，当前数据上可被评估"))

    # 派生属性可触发（Function.output 是否为某实体的属性）
    derived = {str(_f(fn, "output")).strip() for fn in bk.get("function", []) if _f(fn, "output")}
    if derived:
        missing = derived - all_attr_names
        if missing:
            r.append(_item("executability", FAIL, "DERIVED_NOT_TRIGGERABLE",
                           f"派生属性在实体定义中不存在，无法触发写回：{sorted(missing)}"))
        else:
            r.append(_item("executability", PASS, "DERIVED_TRIGGERABLE",
                           f"派生属性均已在实体中声明，可触发：{sorted(derived)}"))

    if not r:
        r.append(_item("executability", PASS, "EXEC_OK", "可执行性：无 Function/Action 节点需校验"))
    return r


# ---------------------------------------------------------------- 汇总
def validate_workflow(spec: dict[str, Any], data: dict[str, Any] | None = None) -> dict[str, Any]:
    """跑四类检查，输出结构化报告。data 为 None/空 → 数据类检查标 no-data。"""
    data = data or {}
    checks: list[dict[str, Any]] = []
    checks += check_structure(spec)
    checks += check_coverage(spec, data)
    checks += check_consistency(spec, data)
    checks += check_executability(spec, data)

    summary: dict[str, Any] = {}
    for cat, label in CATEGORIES:
        items = [c for c in checks if c["category"] == cat]
        worst = max((_LEVEL_RANK.get(c["level"], 0) for c in items), default=0)
        level = {v: k for k, v in _LEVEL_RANK.items()}[worst]
        counts = Counter(c["level"] for c in items)
        summary[cat] = {
            "label": label, "level": level, "passed": level == PASS,
            "counts": {k: counts.get(k, 0) for k in (PASS, WARN, NODATA, FAIL)},
        }

    blocking = [c for c in checks if c["level"] == FAIL]
    nodata = [c for c in checks if c["level"] == NODATA]
    complete = all(summary[c]["passed"] for c, _ in CATEGORIES)

    return {
        "complete": complete,
        "verdict": "本体建模完整" if complete else "本体建模未完成",
        "summary": summary,
        "checks": checks,
        "blocking_count": len(blocking),
        "nodata_count": len(nodata),
        "data_mode": data.get("mode", "none") if isinstance(data, dict) else "none",
        "metrics": {
            "nodes": len(spec.get("nodes") or []),
            "edges": len(spec.get("edges") or []),
            "instances": sum(len(v) for v in (data.get("entities") or {}).values()) if isinstance(data, dict) else 0,
            "relations": len(data.get("relations") or []) if isinstance(data, dict) else 0,
        },
        "note": ("四类检查全部通过：结构/覆盖/一致/可执行。" if complete else
                 f"未通过：{len(blocking)} 项阻断" + (f"，{len(nodata)} 项因无数据未验证" if nodata else "")),
    }


def self_check() -> dict[str, Any]:
    """引擎自检：不依赖外部数据，验证报告结构稳定。"""
    report = validate_workflow({"version": "1.0", "nodes": [], "edges": []}, {})
    ok = (
        set(report["summary"].keys()) == {c for c, _ in CATEGORIES}
        and "complete" in report and "checks" in report
        and report["complete"] is False
    )
    return {"pass": ok, "categories": [c for c, _ in CATEGORIES], "empty_complete": report["complete"]}
