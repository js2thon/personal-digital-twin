"""LinkML 单一建模来源（SIL-C01 / SIL-C02）：序列化与解析互为逆运算。

约束：Schema Registry 只承认 **LinkML 作为建模输入**，代码里的 schema dict 是
它的运行时投影。所以 `dumps` 与 `parse` 必须能闭环 —— 导出再导入要还原同样的
建模语义，否则「单一来源」就退化成两份互相抄的副本（这正是 P1-2 要修的双轨）。

实现取舍：**不引入 PyYAML 硬依赖**。这里自带一个 YAML 子集解析器，覆盖
Registry 实际用到的结构（嵌套映射 / 标量序列 / 行内 `[..]` `{..}` / 引号 / 注释）。
环境里装了 PyYAML 时，测试会做交叉校验，确保子集解析器与标准实现的读法一致。

对外接口：
* `dumps(schema)` / `to_linkml(schema)`：schema dict → LinkML YAML
* `parse(text)` / `loads(text)`：LinkML YAML → schema dict（缺 classes 直接报错）
* `roundtrip(schema)`：无损往返自证，返回逐段差异
* `MODELING_KEYS`：参与建模语义比较的字段（出版时间戳等不参与）
"""
from __future__ import annotations

import json
import re

# 参与「语义相等」比较的字段。created_at / published_by 属出版元数据，
# 每次发布都会变，不该让往返判定失败；status / pinned_by_cq 是治理状态，要算。
MODELING_KEYS = ("schema_version", "entity_types", "relation_types",
                 "attributes", "constraints", "validation_rules",
                 "status", "pinned_by_cq")

_PLAIN = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./\-]*$")
_YAML_WORDS = {"true", "false", "null", "yes", "no", "on", "off", "~", ""}
_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+\.\d+$")

# LinkML 里由本模块映射到 schema 语义的键，其余键原样保留在 entity_types 上
_CLASS_KNOWN = ("description", "is_a", "aliases", "attributes", "slots",
                "annotations", "class_uri", "id_prefixes")


# ------------------------------------------------------------------ 标量编码
def _scalar(v) -> str:
    """安全标量：能用 plain 就用 plain，否则 JSON 双引号（YAML 双引号串 = JSON 串）。"""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    s = str(v)
    if s and _PLAIN.match(s) and s.lower() not in _YAML_WORDS:
        return s
    return json.dumps(s, ensure_ascii=False)


def _flow_list(items: list) -> str:
    return "[" + ", ".join(_scalar(x) for x in items) + "]"


def _flow_map(d: dict) -> str:
    return "{" + ", ".join(f"{_scalar(k)}: {_scalar(v)}" for k, v in d.items()) + "}"


def _emit(obj: dict, indent: int, out: list[str]) -> None:
    pad = "  " * indent
    for key, val in obj.items():
        if isinstance(val, dict):
            if val:
                out.append(f"{pad}{key}:")
                _emit(val, indent + 1, out)
            else:
                out.append(f"{pad}{key}: {{}}")
        elif isinstance(val, (list, tuple)):
            if not val:
                out.append(f"{pad}{key}: []")
            elif all(not isinstance(x, (dict, list, tuple)) for x in val) and len(val) > 1:
                out.append(f"{pad}{key}:")
                for x in val:
                    out.append(f"{pad}  - {_scalar(x)}")
            elif all(not isinstance(x, (dict, list, tuple)) for x in val):
                out.append(f"{pad}{key}: {_flow_list(list(val))}")
            else:
                out.append(f"{pad}{key}: {_flow_list(list(val))}")
        else:
            out.append(f"{pad}{key}: {_scalar(val)}")


# ------------------------------------------------------------------ 序列化
def dumps(schema: dict | None, *, header: bool = True) -> str:
    """schema dict → LinkML YAML；输出可被 `parse` 无损读回。"""
    s = dict(schema or {})
    out: list[str] = []
    if header:
        out.append("# LinkML 单一建模来源（Schema Registry 导出）")
        out.append("# 手改无效：改动请走 POST /api/v1/ontology/v2/schema/publish 或 /schema/linkml/import")
    out.append("id: https://example.org/ontology/cq-v2")
    out.append("name: EnterpriseOntology")
    out.append(f"version: {_scalar(s.get('schema_version') or '')}")
    out.append("prefixes:")
    out.append("  linkml: https://w3id.org/linkml/")
    out.append("default_range: string")

    entity_types = s.get("entity_types") or {}
    attributes = s.get("attributes") or {}
    out.append("classes:" if entity_types else "classes: {}")
    for name in sorted(entity_types):
        meta = entity_types[name] if isinstance(entity_types[name], dict) else {}
        out.append(f"  {_scalar(name)}:")
        out.append(f"    description: {_scalar(meta.get('label') or name)}")
        if meta.get("parent"):
            out.append(f"    is_a: {_scalar(meta['parent'])}")
        aliases = list(meta.get("aliases") or [])
        if aliases:
            out.append("    aliases:")
            for a in aliases:
                out.append(f"      - {_scalar(a)}")
        owned = [k for k in sorted(attributes)
                 if str((attributes[k] or {}).get("owner") or "") == name]
        if owned:
            out.append("    attributes:")
            for attr in owned:
                ameta = {k: v for k, v in dict(attributes[attr] or {}).items()
                         if k != "owner"}
                out.append(f"      {_scalar(attr)}:")
                _emit(ameta or {"range": "string"}, 4, out)
        extra = {k: v for k, v in meta.items()
                 if k not in ("label", "aliases", "parent")}
        if extra:
            out.append("    annotations:")
            _emit(extra, 3, out)

    relation_types = s.get("relation_types") or {}
    out.append("relation_types:" if relation_types else "relation_types: {}")
    for name in sorted(relation_types):
        meta = relation_types[name] if isinstance(relation_types[name], dict) else {}
        out.append(f"  {_scalar(name)}:")
        _emit(meta or {"label": name}, 2, out)

    orphans = {k: {kk: vv for kk, vv in dict(v or {}).items() if kk != "owner"}
               for k, v in attributes.items()
               if not (v or {}).get("owner")}
    settings = {
        "constraints": s.get("constraints") or {},
        "validation_rules": s.get("validation_rules") or {},
        "status": s.get("status") or "published",
        "pinned_by_cq": list(s.get("pinned_by_cq") or []),
    }
    if orphans:
        settings["orphan_attributes"] = orphans
    out.append("settings:")
    _emit(settings, 1, out)
    return "\n".join(out) + "\n"


to_linkml = dumps


# ------------------------------------------------------------------ YAML 子集
def _strip_comment(line: str) -> str:
    out, quote, i = [], None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _split_kv(s: str) -> tuple[str, str]:
    quote = None
    for idx, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ":":
            return _key(s[:idx]), s[idx + 1:].strip()
    raise ValueError(f"YAML 子集解析失败：缺冒号 → {s!r}")


def _key(raw: str):
    k = raw.strip()
    if len(k) >= 2 and k[0] == k[-1] and k[0] in "\"'":
        return _scalar(k)
    return k


def _split_flow(s: str) -> list[str]:
    parts, buf, quote, depth = [], [], None, 0
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _scalar_value(s: str):
    s = s.strip()
    if s == "":
        return ""
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    low = s.lower()
    if low in ("null", "~"):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if _INT.match(s):
        return int(s)
    if _FLOAT.match(s):
        return float(s)
    return s


def _value(s: str):
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        return [_value(x) for x in _split_flow(s[1:-1])]
    if s.startswith("{") and s.endswith("}"):
        out = {}
        for part in _split_flow(s[1:-1]):
            if not part:
                continue
            k, rest = _split_kv(part)
            out[k] = _value(rest) if rest != "" else None
        return out
    return _scalar_value(s)


def _parse_block(lines: list[tuple[int, str]], i: int, indent: int):
    if lines[i][1].startswith("- "):
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_list(lines: list[tuple[int, str]], i: int, indent: int):
    out = []
    while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("-"):
        item = lines[i][1][1:].strip()
        i += 1
        if item == "" and i < len(lines) and lines[i][0] > indent:
            child, i = _parse_block(lines, i, lines[i][0])
            out.append(child)
        else:
            out.append(_value(item))
    return out, i


def _parse_map(lines: list[tuple[int, str]], i: int, indent: int):
    out: dict = {}
    while i < len(lines):
        ind, text = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise ValueError(f"YAML 子集解析失败：缩进异常 → {text!r}")
        key, rest = _split_kv(text)
        i += 1
        if rest == "":
            if i < len(lines) and lines[i][0] > indent:
                val, i = _parse_block(lines, i, lines[i][0])
            else:
                val = None
        else:
            val = _value(rest)
        out[key] = val
    return out, i


def parse_yaml(text: str):
    lines: list[tuple[int, str]] = []
    for raw in (text or "").splitlines():
        line = _strip_comment(raw.rstrip())
        if not line.strip():
            continue
        lines.append((len(line) - len(line.lstrip(" ")), line.strip()))
    if not lines:
        raise ValueError("LinkML 解析失败：空文档")
    doc, _ = _parse_block(lines, 0, lines[0][0])
    return doc


# ------------------------------------------------------------------ 解析
def parse(text: str) -> dict:
    """LinkML YAML → schema dict。缺 `classes` 直接报错，不静默产出空本体。"""
    doc = parse_yaml(text)
    if not isinstance(doc, dict):
        raise ValueError("LinkML 解析失败：顶层不是映射")
    if "classes" not in doc:
        raise ValueError("LinkML 解析失败：缺少 classes 段（本体至少要声明一个类）")

    entity_types: dict[str, dict] = {}
    attributes: dict[str, dict] = {}
    slots = doc.get("slots") or {}
    for name, raw in (doc.get("classes") or {}).items():
        meta = dict(raw) if isinstance(raw, dict) else {}
        et = {
            "label": str(meta.get("description") or name),
            "aliases": [str(a) for a in (meta.get("aliases") or [])],
            "parent": str(meta.get("is_a") or ""),
        }
        for k, v in (meta.get("annotations") or {}).items():
            et[str(k)] = v
        for k, v in meta.items():
            if k not in _CLASS_KNOWN:
                et[str(k)] = v
        entity_types[str(name)] = et

        for aname, ameta in (meta.get("attributes") or {}).items():
            spec = dict(ameta) if isinstance(ameta, dict) else {"range": "string"}
            spec.setdefault("range", "string")
            spec["owner"] = str(name)
            attributes[str(aname)] = spec
        for sname in (meta.get("slots") or []):
            spec = dict(slots.get(sname) or {}) if isinstance(slots.get(sname), dict) else {}
            spec.setdefault("range", "string")
            spec["owner"] = str(name)
            attributes[str(sname)] = spec

    settings = doc.get("settings") if isinstance(doc.get("settings"), dict) else {}
    out = {
        "schema_version": str(doc.get("version") or doc.get("schema_version") or ""),
        "entity_types": entity_types,
        "relation_types": {str(k): (dict(v) if isinstance(v, dict) else {})
                           for k, v in (doc.get("relation_types") or {}).items()},
        "attributes": attributes,
        "constraints": dict(settings.get("constraints")
                            or doc.get("constraints") or {}),
        "validation_rules": dict(settings.get("validation_rules")
                                 or doc.get("validation_rules") or {}),
        "status": str(settings.get("status") or doc.get("status") or "published"),
        "pinned_by_cq": [str(x) for x in (settings.get("pinned_by_cq") or [])],
    }
    for aname, ameta in (settings.get("orphan_attributes") or {}).items():
        spec = dict(ameta) if isinstance(ameta, dict) else {"range": "string"}
        spec.setdefault("range", "string")
        spec.setdefault("owner", "")
        out["attributes"].setdefault(str(aname), spec)
    return out


loads = parse


# ------------------------------------------------------------------ 自证
def _canon(schema: dict | None) -> dict:
    s = dict(schema or {})
    ets = {}
    for k, v in (s.get("entity_types") or {}).items():
        v = v if isinstance(v, dict) else {}
        item = {"label": str(v.get("label") or ""),
                "aliases": [str(x) for x in (v.get("aliases") or [])],
                "parent": str(v.get("parent") or "")}
        for extra, val in v.items():
            if extra not in ("label", "aliases", "parent"):
                item[str(extra)] = val
        ets[str(k)] = item
    attrs = {str(k): dict(v if isinstance(v, dict) else {}) for k, v in
             (s.get("attributes") or {}).items()}
    return {
        "schema_version": str(s.get("schema_version") or ""),
        "entity_types": ets,
        "relation_types": {str(k): dict(v if isinstance(v, dict) else {})
                           for k, v in (s.get("relation_types") or {}).items()},
        "attributes": attrs,
        "constraints": dict(s.get("constraints") or {}),
        "validation_rules": dict(s.get("validation_rules") or {}),
        "status": str(s.get("status") or ""),
        "pinned_by_cq": [str(x) for x in (s.get("pinned_by_cq") or [])],
    }


def _jsonable(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def roundtrip(schema: dict | None) -> dict:
    """导出→导入→比对。不一致就返回差异，绝不假装无损。"""
    text = dumps(schema)
    back = parse(text)
    a, b = _canon(schema), _canon(back)
    diff = {k: {"expected": _jsonable(a[k]), "actual": _jsonable(b[k])}
            for k in MODELING_KEYS if a[k] != b[k]}
    return {"lossless": not diff, "version": a["schema_version"],
            "bytes": len(text.encode("utf-8")),
            "classes": len(a["entity_types"]), "relations": len(a["relation_types"]),
            "attributes": len(a["attributes"]), "diff": diff}


def schema_uri(schema: dict | None) -> str:
    ver = (schema or {}).get("schema_version") or "unversioned"
    return f"https://example.org/ontology/cq-v2/{ver}"
