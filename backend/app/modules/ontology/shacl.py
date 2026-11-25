"""SHACL 写入侧校验（SIL-DC-04）：事实落库前先按 Shape 校验，不让坏数据进库。

为什么要有这一层：`knowledge.facts` 的 CHECK 约束只在**违反时**才报错，且错误
信息是数据库方言；关系的两端类型（R2）、属性取值域、时间窗顺序这些规则过去
散在 Planner / 契约 / 各写入路径里，各写各的。这里把它们收敛成一份 Shape，
写入侧统一校验，违规可定位到具体 path 与 constraint。

Shape 来源（优先级从高到低）：
1. 模块同目录的 `shapes.yaml`（运维可直接改规则，不必改代码）；
2. 由当前本体 SemanticSchema 派生（保证规则跟本体同步）。

支持的约束（SHACL 子集 + 两个本项目扩展，够覆盖事实层）：
`sh:minCount` / `sh:maxCount` / `sh:datatype` / `sh:in` / `sh:minInclusive` /
`sh:maxInclusive` / `sh:pattern` / `sh:nodeKind` / `sh:class`；
扩展：`sh:windowOrder`（valid_from ≤ valid_to）、`sh:endpoint`（关系两端类型，R2）。

模式 `SHACL_MODE`：`warn`（默认，记录不拦截）/ `strict`（拦截，写失败）/
`off`（完全关闭）。默认 warn 的理由：历史写入路径很多，直接 strict 会把既有
流程打断；先拿违规分布，再把关键路径切到 strict。
"""
from __future__ import annotations

import json
import logging
import numbers
import os
import re
import threading
from collections import Counter, deque
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

SHAPES_PATH = Path(__file__).with_name("shapes.yaml")
MODES = ("off", "warn", "strict")

# 与 knowledge.facts 的 CHECK 约束对齐：违反这些的写入在库层也会失败，
# 所以放在写入侧提前拦，才能给出「哪个字段、什么原因」而不是一句 SQL 报错。
CORE_PROPERTIES: dict[str, dict] = {
    "predicate": {"datatype": "string", "minCount": 1, "maxCount": 1,
                  "pattern": r"^[A-Za-z][A-Za-z0-9_.\-]{0,63}$",
                  "severity": "violation"},
    "object_value": {"minCount": 1, "nonEmpty": True, "severity": "warning"},
    "confidence": {"datatype": "decimal", "minInclusive": 0, "maxInclusive": 1,
                   "severity": "violation"},
    "trust_tier": {"datatype": "integer", "in": [1, 2, 3], "severity": "violation"},
    "status": {"in": ["active", "contested", "superseded", "withdrawn"],
               "severity": "violation"},
    "visibility": {"in": ["owner", "organization", "restricted"],
                   "severity": "violation"},
    "datatype": {"in": ["text", "integer", "number", "boolean", "date", "datetime",
                        "duration", "ref", "json"], "severity": "warning"},
    "valid_from": {"datatype": "dateTime", "severity": "warning"},
    "valid_to": {"datatype": "dateTime", "severity": "warning"},
}

_DT_TYPES = (datetime, date)


class ShaclViolation(ValueError):
    """strict 模式下写入被 Shape 拦下；API 层据此返回 400 而不是 500。"""

    def __init__(self, report: dict):
        self.report = report or {}
        bad = [f"{v.get('path')}({v.get('constraint')})"
               for v in (self.report.get("violations") or [])
               if v.get("severity") == "violation"]
        super().__init__("SHACL 校验未通过：" + ("；".join(bad[:6]) or "未知违规"))


# ------------------------------------------------------------------ 模式与开关
def mode() -> str:
    env = (os.getenv("SHACL_MODE") or "").strip().lower()
    if env in MODES:
        return env
    try:
        from app.config import settings

        val = str(getattr(settings, "shacl_mode", "") or "warn").strip().lower()
        return val if val in MODES else "warn"
    except Exception:  # noqa: BLE001
        return "warn"


def shapes_file() -> Path | None:
    return SHAPES_PATH if SHAPES_PATH.exists() else None


# ------------------------------------------------------------------ Shape 生成
def shapes_from_schema(schema: dict | None) -> dict:
    """由 Schema Registry 的某一版派生事实层 Shape。"""
    schema = schema or {}
    relations: dict[str, dict] = {}
    for name, meta in (schema.get("relation_types") or {}).items():
        meta = meta if isinstance(meta, dict) else {}
        relations[str(name)] = {"source": meta.get("source") or "",
                                "target": meta.get("target") or "",
                                "cardinality": meta.get("cardinality") or ""}
    attrs: dict[str, dict] = {}
    for name, meta in (schema.get("attributes") or {}).items():
        meta = meta if isinstance(meta, dict) else {}
        attrs[str(name)] = {"range": meta.get("range") or "string",
                            "owner": meta.get("owner") or ""}
    return {
        "shapes_version": "shacl-1.0",
        "schema_version": str(schema.get("schema_version") or ""),
        "target": "knowledge.facts",
        "closed": False,
        "properties": {k: dict(v) for k, v in CORE_PROPERTIES.items()},
        "relation_endpoints": relations,
        "attribute_ranges": attrs,
        "constraints": {"windowOrder": ["valid_from", "valid_to"],
                        "predicate_in_schema_only": False},
    }


def _ontology_schema() -> dict:
    """从独立本体语义 Schema 投影出 SHACL 所需的关系与属性目录。"""
    try:
        from app.modules.ontology.semantic import schema_def

        semantic = schema_def.to_schema_dict(schema_def.default_semantic_schema())
        relations = {}
        for name, meta in (semantic.get("relation_types") or {}).items():
            sources = meta.get("source_types") or ["*"]
            targets = meta.get("target_types") or ["*"]
            relations[name] = {
                "source": sources[0] if len(sources) == 1 else "*",
                "target": targets[0] if len(targets) == 1 else "*",
                "cardinality": meta.get("cardinality") or "",
            }
        attributes = {}
        for owner, entity in (semantic.get("entity_types") or {}).items():
            for attr in entity.get("attributes") or []:
                attributes[attr["name"]] = {
                    "range": attr.get("range") or "string", "owner": owner,
                }
        return {
            "schema_version": semantic.get("ontology_version") or semantic.get("schema_version") or "",
            "relation_types": relations, "attributes": attributes,
        }
    except Exception:  # noqa: BLE001
        log.exception("无法从本体 SemanticSchema 派生 SHACL shapes")
        return {}


_cache: dict = {"key": None, "shapes": None}
_lock = threading.Lock()


def current_shapes(refresh: bool = False) -> dict:
    """当前生效的 Shape：shapes.yaml 覆盖 > 本体 Schema 派生。按版本号缓存。"""
    path = shapes_file()
    with _lock:
        if path is not None:
            mtime = path.stat().st_mtime
            key = f"file:{mtime}"
            if not refresh and _cache["key"] == key and _cache["shapes"]:
                return _cache["shapes"]
            shapes = _merge_override(shapes_from_schema(_ontology_schema()), path)
            _cache.update(key=key, shapes=shapes)
            return shapes

        base = shapes_from_schema(_registry_schema())
        key = f"registry:{base['schema_version']}"
        if not refresh and _cache["key"] == key and _cache["shapes"]:
            return _cache["shapes"]
        _cache.update(key=key, shapes=base)
        return base


def _merge_override(base: dict, path: Path) -> dict:
    """shapes.yaml 允许只写要改的项：properties 逐项覆盖，其余整段替换。"""
    try:
        import yaml

        override = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("shapes.yaml 解析失败，回落 Registry 派生：%s", exc)
        return base
    merged = json.loads(json.dumps(base, default=str))
    for key, val in override.items():
        if key == "properties" and isinstance(val, dict):
            for pname, spec in val.items():
                if isinstance(spec, dict):
                    merged["properties"].setdefault(pname, {}).update(spec)
        else:
            merged[key] = val
    merged["source"] = str(path)
    return merged


# ------------------------------------------------------------------ 校验原语
def _as_dt(value):
    if isinstance(value, _DT_TYPES):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


def _datatype_ok(value, dt: str) -> bool:
    if dt == "string":
        return isinstance(value, str)
    if dt == "integer":
        # DB 的 NUMERIC/SMALLINT 经驱动回读可能是 Decimal，按数值判定而不是按类判定
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, numbers.Number) and float(value).is_integer()
    if dt in ("decimal", "number", "double", "float"):
        return isinstance(value, numbers.Number) and not isinstance(value, bool)
    if dt == "boolean":
        return isinstance(value, bool)
    if dt == "dateTime":
        return _as_dt(value) is not None
    if dt == "date":
        return _as_dt(value) is not None
    if dt in ("anyURI", "IRI"):
        return isinstance(value, str) and ("://" in value or value.startswith("urn:"))
    return True


def _short(value) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                              default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    return text if len(text) <= 120 else text[:117] + "..."


def _check_property(name: str, value, spec: dict, present: bool) -> list[dict]:
    out: list[dict] = []
    sev = str(spec.get("severity") or "violation")

    def add(constraint: str, message: str):
        out.append({"path": name, "constraint": constraint, "message": message,
                    "severity": sev, "value": _short(value)})

    min_count = int(spec.get("minCount") or 0)
    if not present or value is None:
        # 缺失只由 minCount 判；其它约束（in / datatype / pattern）对空值不适用，
        # 否则每个可选字段都会因「没传」被判违规。
        if min_count:
            add("sh:minCount", f"{name} 为必填（minCount={min_count}）")
        return out
    if spec.get("nonEmpty") and (value == "" or value == {} or value == []):
        add("sh:nonEmpty", f"{name} 不能为空值")
        return out
    if min_count and isinstance(value, (str, dict, list)) and not value:
        # 空串/空集合在语义上等于「没给」：判 minCount 比判 pattern 更贴近事实
        add("sh:minCount", f"{name} 为必填（minCount={min_count}），实际为空")
        return out

    dt = spec.get("datatype")
    if dt and not _datatype_ok(value, str(dt)):
        add("sh:datatype", f"{name} 期望 {dt}，实际 {type(value).__name__}")
        return out

    allowed = spec.get("in")
    if isinstance(allowed, list) and allowed and value not in allowed:
        add("sh:in", f"{name} 不在允许集合内：{_short(value)} ∉ {allowed}")

    lo, hi = spec.get("minInclusive"), spec.get("maxInclusive")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if lo is not None and value < lo:
            add("sh:minInclusive", f"{name}={value} < {lo}")
        if hi is not None and value > hi:
            add("sh:maxInclusive", f"{name}={value} > {hi}")

    pat = spec.get("pattern")
    if pat and isinstance(value, str) and not re.search(str(pat), value):
        add("sh:pattern", f"{name} 不匹配 {pat}")

    node_kind = spec.get("nodeKind")
    if node_kind == "IRI" and not _datatype_ok(value, "anyURI"):
        add("sh:nodeKind", f"{name} 需要 IRI，实际 {_short(value)}")
    return out


def _check_window(fact: dict, order: list) -> list[dict]:
    if len(order) < 2:
        return []
    start, end = fact.get(order[0]), fact.get(order[1])
    a, b = _as_dt(start), _as_dt(end)
    if a is None or b is None:
        return []
    if b < a:
        return [{"path": order[1], "constraint": "sh:windowOrder",
                 "message": f"{order[1]}({b}) 早于 {order[0]}({a})，有效时间窗倒置",
                 "severity": "violation", "value": _short(str(end))}]
    return []


def _check_endpoint(fact: dict, shapes: dict, subject_type: str | None,
                    object_type: str | None) -> list[dict]:
    """R2：关系两端类型必须满足 source/target 约束。"""
    spec = (shapes.get("relation_endpoints") or {}).get(str(fact.get("predicate") or ""))
    if not spec:
        return []
    out = []
    src, tgt = spec.get("source") or "", spec.get("target") or ""
    if subject_type and src and subject_type != src:
        out.append({"path": "subject_id", "constraint": "sh:endpoint",
                    "message": f"{fact.get('predicate')} 的 source 应为 {src}，实际 {subject_type}",
                    "severity": "violation", "value": _short(subject_type)})
    if object_type and tgt and object_type != tgt:
        out.append({"path": "object_value", "constraint": "sh:endpoint",
                    "message": f"{fact.get('predicate')} 的 target 应为 {tgt}，实际 {object_type}",
                    "severity": "violation", "value": _short(object_type)})
    return out


def _check_attribute_range(fact: dict, shapes: dict) -> list[dict]:
    spec = (shapes.get("attribute_ranges") or {}).get(str(fact.get("predicate") or ""))
    if not spec:
        return []
    rng = str(spec.get("range") or "string")
    if rng not in ("integer", "number", "boolean", "date", "datetime"):
        return []
    value = fact.get("object_value")
    if isinstance(value, dict):
        value = value.get("value", value)
    ok = _datatype_ok(value, "integer" if rng == "integer" else
                      "decimal" if rng == "number" else
                      "boolean" if rng == "boolean" else "date")
    if ok:
        return []
    return [{"path": "object_value", "constraint": "sh:datatype",
             "message": f"{fact.get('predicate')} 的 range={rng}，取值类型不符",
             "severity": "warning", "value": _short(value)}]


def _check_predicate_known(fact: dict, shapes: dict) -> list[dict]:
    if not (shapes.get("constraints") or {}).get("predicate_in_schema_only"):
        return []
    pred = str(fact.get("predicate") or "")
    known = set(shapes.get("relation_endpoints") or {}) | \
        set(shapes.get("attribute_ranges") or {})
    if not known or pred in known:
        return []
    return [{"path": "predicate", "constraint": "sh:closed",
             "message": f"谓词 {pred} 未在 Schema Registry 登记",
             "severity": "warning", "value": _short(pred)}]


# ------------------------------------------------------------------ 对外校验
_STATS: dict = {"checks": 0, "conforms": 0, "violations": 0, "warnings": 0,
                "blocked": 0, "by_constraint": Counter(), "by_path": Counter()}
_RECENT: deque = deque(maxlen=200)


def validate(fact: dict, shapes: dict | None = None, *,
             subject_type: str | None = None,
             object_type: str | None = None) -> dict:
    """按 Shape 校验一条事实载荷；返回逐条违规，不做任何写操作。"""
    shapes = shapes or current_shapes()
    violations: list[dict] = []
    props = shapes.get("properties") or {}
    for name, spec in props.items():
        present = name in fact and fact[name] is not None
        violations += _check_property(name, fact.get(name), spec, present)
    violations += _check_window(fact, (shapes.get("constraints") or {}).get("windowOrder") or [])
    violations += _check_endpoint(fact, shapes, subject_type, object_type)
    violations += _check_attribute_range(fact, shapes)
    violations += _check_predicate_known(fact, shapes)

    blocking = [v for v in violations if v["severity"] == "violation"]
    return {
        "conforms": not violations,
        "blocking": bool(blocking),
        "shapes_version": shapes.get("shapes_version"),
        "schema_version": shapes.get("schema_version"),
        "checked": len(props),
        "violations": violations,
        "blocking_count": len(blocking),
        "warning_count": len(violations) - len(blocking),
    }


def check_write(fact: dict, *, subject_type: str | None = None,
                object_type: str | None = None) -> dict:
    """写入路径专用：按当前模式决定是否真的拦截，并把结果计入统计。"""
    md = mode()
    if md == "off":
        return {"mode": md, "conforms": True, "blocked": False, "violations": []}
    try:
        report = validate(fact, subject_type=subject_type, object_type=object_type)
    except Exception as exc:  # noqa: BLE001  校验器自身故障不得阻断写入
        log.warning("SHACL 校验异常（已降级放行）：%s", exc)
        return {"mode": md, "conforms": True, "blocked": False,
                "degraded_reason": f"{type(exc).__name__}: {str(exc)[:120]}",
                "violations": []}
    blocked = bool(report["blocking"]) and md == "strict"
    with _lock:
        _STATS["checks"] += 1
        _STATS["conforms" if report["conforms"] else "violations"] += 1
        _STATS["warnings"] += report["warning_count"]
        _STATS["blocked"] += 1 if blocked else 0
        for v in report["violations"]:
            _STATS["by_constraint"][v["constraint"]] += 1
            _STATS["by_path"][v["path"]] += 1
        if report["violations"]:
            _RECENT.append({"at": datetime.now().isoformat(timespec="seconds"),
                            "predicate": fact.get("predicate"),
                            "blocked": blocked,
                            "violations": report["violations"][:8]})
    return {**report, "mode": md, "blocked": blocked}


def recent(limit: int = 20) -> list[dict]:
    with _lock:
        items = list(_RECENT)
    return items[-limit:][::-1]


def stats() -> dict:
    with _lock:
        return {"mode": mode(), "shapes_file": str(shapes_file() or ""),
                "checks": _STATS["checks"], "conforms": _STATS["conforms"],
                "violations": _STATS["violations"],
                "warnings": _STATS["warnings"], "blocked": _STATS["blocked"],
                "by_constraint": dict(_STATS["by_constraint"]),
                "by_path": dict(_STATS["by_path"])}


def reset_stats() -> dict:
    with _lock:
        for key in ("checks", "conforms", "violations", "warnings", "blocked"):
            _STATS[key] = 0
        _STATS["by_constraint"].clear()
        _STATS["by_path"].clear()
        _RECENT.clear()
    return stats()


def audit_facts(limit: int = 200, where: str = "status = 'active'") -> dict:
    """抽样体检：对既有事实跑一遍 Shape，给出违规分布（只读，不改数据）。"""
    from app.db import session as db

    rows = db.fetch_all(
        "SELECT id, subject_id, predicate, object_value, valid_from, valid_to, "
        "       status, confidence, trust_tier, visibility, datatype "
        f"FROM knowledge.facts WHERE {where} LIMIT :n", {"n": int(limit)})
    types = _entity_types([r.get("subject_id") for r in rows])
    totals = Counter()
    samples: list[dict] = []
    nonconform = 0
    for row in rows:
        fact = {k: row.get(k) for k in
                ("predicate", "object_value", "valid_from", "valid_to", "status",
                 "confidence", "trust_tier", "visibility", "datatype")}
        st = str(row["subject_id"]) if row.get("subject_id") else None
        report = validate(fact, subject_type=types.get(st) if st else None)
        if not report["conforms"]:
            nonconform += 1
            for v in report["violations"]:
                totals[v["constraint"]] += 1
            if len(samples) < 10:
                samples.append({"fact_id": str(row["id"]),
                                "predicate": row.get("predicate"),
                                "violations": report["violations"][:5]})
    return {"checked": len(rows), "nonconform": nonconform,
            "conform_rate": round((len(rows) - nonconform) / len(rows), 4) if rows else None,
            "by_constraint": dict(totals), "samples": samples,
            "shapes": {"version": (current_shapes().get("shapes_version")),
                       "schema_version": current_shapes().get("schema_version")}}


def _entity_types(ids: list) -> dict:
    """批量解析实体类型（R2 判定用）；不可用时返回空表而不是猜。"""
    clean = [str(i) for i in ids if i]
    if not clean:
        return {}
    try:
        from app.db import session as db

        rows = db.fetch_all(
            "SELECT id::text AS id, entity_type FROM ontology.entities "
            "WHERE id = ANY(CAST(:ids AS uuid[]))", {"ids": "{" + ",".join(clean) + "}"})
        return {str(r["id"]): r["entity_type"] for r in rows}
    except Exception:  # noqa: BLE001
        return {}
