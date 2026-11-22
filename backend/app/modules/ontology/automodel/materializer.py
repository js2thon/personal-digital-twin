"""④ 物化：按映射契约把数据变成 实体 + 事实（tier2，不经 LLM）。

输出与现有链路对齐：
- entities: {external_id, entity_type, canonical_name, properties}
- facts:    {subject_external_id, predicate, object_value, trust_tier, source_ref}
  关系事实的 object_value = {"external_id": ...}，正好命中 projection.py 的
  pick_object_external_id 路径 —— 不再依赖名字匹配。

物化结果仅用于 Discovery 预览与质量分析；旧 `PgWriter` 已禁用。
本模块接收的 TableSet 属于 Automodel Discovery 数据集，永远不能作为生产摄取凭据。
"""
from __future__ import annotations

import math
import re

import pandas as pd

from .connectors import TableSet

DISCOVERY_DATA_SCOPE = "discovery_sample"


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if hasattr(v, "item"):
        return v.item()
    return v


_FIELD = re.compile(r"\{([A-Za-z0-9_\u4e00-\u9fff]+)\}")


def _fields(tpl: str) -> list[str]:
    return _FIELD.findall(tpl)


def render(tpl: str, values: dict) -> str | None:
    """安全模板替换：只认 {name}，不支持属性/索引访问（避免 str.format 注入）。"""
    missing = False

    def sub(m):
        nonlocal missing
        v = _norm(values.get(m.group(1)))
        if v is None:
            missing = True
            return ""
        return str(v)
    out = _FIELD.sub(sub, tpl)
    return None if missing else out


def _fmt(tpl: str, row: dict) -> str | None:
    return render(tpl, row)


def _norm(v):
    v = _clean(v)
    return int(v) if isinstance(v, float) and v.is_integer() else v


def materialize(ts: TableSet, contract: dict) -> dict:
    src = contract["source"]
    entities, facts, errors = [], [], []
    known: set[str] = set()
    for m in contract["mappings"]:
        df = ts.tables[m["table"]].df
        if not m["id"]:
            errors.append({"table": m["table"], "error": "契约缺 id 模板，跳过"})
            continue
        for row in df.to_dict(orient="records"):
            ext = _fmt(m["id"], row)
            if not ext:
                errors.append({"table": m["table"], "error": "主键为空", "row": str(row)[:120]})
                continue
            name = _clean(row.get(m["name"])) if m.get("name") else None
            props = {a: _clean(row.get(col)) for a, col in m["attributes"].items()}
            entities.append({"external_id": ext, "entity_type": m["class"],
                             "canonical_name": str(name) if name is not None else ext,
                             "properties": {k: v for k, v in props.items() if v is not None}})
            known.add(ext)
            ref = {"origin": "structured_mapping", "source": src, "table": m["table"],
                   "contract_version": contract["contract_version"], "source_row_key": ext}
            for a, v in props.items():
                if v is not None:
                    facts.append({"subject_external_id": ext, "predicate": a,
                                  "object_value": {"value": v}, "trust_tier": 2,
                                  "source_ref": {**ref, "column": m["attributes"][a]}})
            for r in m["relations"]:
                v = _norm(row.get(r["column"]))
                if v is None:
                    continue
                facts.append({"subject_external_id": ext, "predicate": r["type"],
                              "object_value": {"external_id": render(r["target_id"], {"value": v})},
                              "trust_tier": 2,
                              "source_ref": {**ref, "column": r["column"]}})
    for lk in contract.get("links", []):
        df = ts.tables[lk["table"]].df
        for row in df.to_dict(orient="records"):
            a, b = _norm(row.get(lk["source_column"])), _norm(row.get(lk["target_column"]))
            if a is None or b is None:
                continue
            facts.append({"subject_external_id": f"{src}:{lk['source_table']}:{a}",
                          "predicate": lk["type"],
                          "object_value": {"external_id": f"{src}:{lk['target_table']}:{b}"},
                          "trust_tier": 2, "multi": True,
                          "source_ref": {"origin": "structured_mapping", "source": src,
                                         "table": lk["table"], "column": lk["source_column"],
                                         "target_column": lk["target_column"]}})
    # 悬空引用（目标不存在）单列，不静默丢
    dangling = [f for f in facts if "external_id" in f["object_value"]
                and f["object_value"]["external_id"] not in known]
    rel_facts = [f for f in facts if "external_id" in f["object_value"]]
    linked = {f["subject_external_id"] for f in rel_facts} | \
             {f["object_value"]["external_id"] for f in rel_facts}
    isolated = [e["external_id"] for e in entities if e["external_id"] not in linked]
    return {"data_scope": DISCOVERY_DATA_SCOPE,
            "entities": entities, "facts": facts, "errors": errors[:200],
            "dangling": dangling[:200],
            "stats": {"entities": len(entities), "facts": len(facts),
                      "relation_facts": len(rel_facts), "dangling": len(dangling),
                      "isolated_entities": len(isolated),
                      "isolated_ratio": round(len(isolated) / len(entities), 3) if entities else 0}}


def _datatype(ov: dict) -> str:
    v = ov.get("value")
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(T.*)?", v):
        return "date" if len(v) == 10 else "datetime"
    return "text"


class PgWriter:
    """已禁用的旧 Automodel writer；生产摄取统一走 ontology.mapping_execution。"""

    def __init__(self, db):
        self.db = db

    def write(self, result: dict, visibility: str = "owner",
              ontology_version: str | None = None, tenant_id: str = "default") -> dict:
        # 该 writer 属于旧 Automodel 样本物化路径。生产写入已由
        # ontology.mapping_execution 承担（审核映射、事务、租户约束、摄取运行记录）。
        # 不接受调用方传入的 data_scope 标记作为“全量数据”证明，避免样本被伪装成生产数据。
        raise ValueError(
            "Automodel PgWriter 已禁用：Discovery 数据仅用于画像/本体构建/预览；"
            "生产全量或增量写入必须使用已审核的 ontology.mapping_execution 流程"
        )
