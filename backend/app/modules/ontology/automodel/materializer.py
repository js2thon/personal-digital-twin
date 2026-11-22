"""④ 物化：按映射契约把数据变成 实体 + 事实（tier2，不经 LLM）。

输出与现有链路对齐：
- entities: {external_id, entity_type, canonical_name, properties}
- facts:    {subject_external_id, predicate, object_value, trust_tier, source_ref}
  关系事实的 object_value = {"external_id": ...}，正好命中 projection.py 的
  pick_object_external_id 路径 —— 不再依赖名字匹配。

writer 可注入：默认只返回结果（便于测试）；`PgWriter` 写入 PG 并交给投影/Outbox。
"""
from __future__ import annotations

import math
import re

import pandas as pd

from .connectors import TableSet


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
    return {"entities": entities, "facts": facts, "errors": errors[:200],
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
    """写入 ontology.entities / knowledge.facts。按 external_id 幂等 upsert。

    依赖：ontology.entities 上需有 (properties->'external_ids') 的 GIN 索引（jobs.ensure_schema 会建），
    以及 knowledge.facts 写入后由触发器发 Outbox 事件（现有链路）。
    """

    def __init__(self, db):
        self.db = db

    def write(self, result: dict, visibility: str = "owner",
              ontology_version: str | None = None, tenant_id: str = "default") -> dict:
        import json
        ids: dict[str, str] = {}
        for e in result["entities"]:
            row = self.db.fetch_one(
                "SELECT id FROM ontology.entities WHERE properties->'external_ids' @> CAST(:x AS jsonb) "
                "AND status = 'active' LIMIT 1",
                {"x": json.dumps([e["external_id"]])})
            props = {**e["properties"], "external_ids": [e["external_id"]]}
            if row:
                self.db.execute(
                    "UPDATE ontology.entities SET canonical_name=:n, entity_type=:t, "
                    "properties = properties || CAST(:p AS jsonb) WHERE id = CAST(:i AS uuid)",
                    {"n": e["canonical_name"], "t": e["entity_type"],
                     "p": json.dumps(props, ensure_ascii=False, default=str), "i": str(row["id"])})
                ids[e["external_id"]] = str(row["id"])
            else:
                ids[e["external_id"]] = str(self.db.insert_returning_id("ontology.entities", {
                    "canonical_name": e["canonical_name"], "entity_type": e["entity_type"],
                    "status": "active",
                    "properties": json.dumps(props, ensure_ascii=False, default=str)}))
        n = 0
        for f in result["facts"]:
            sid = ids.get(f["subject_external_id"])
            if not sid:
                continue
            if not f.get("multi"):   # 单值谓词：旧结构化事实置为 superseded（双时间线）；多对多不覆盖
                self.db.execute(
                    "UPDATE knowledge.facts SET status='superseded', updated_at=now() "
                    "WHERE subject_id = CAST(:s AS uuid) AND predicate = :p AND status='active' "
                    "AND source_ref->>'origin'='structured_mapping' AND object_value <> CAST(:o AS jsonb)",
                    {"s": sid, "p": f["predicate"], "o": json.dumps(f["object_value"], default=str)})
            exists = self.db.fetch_one(
                "SELECT 1 FROM knowledge.facts WHERE subject_id = CAST(:s AS uuid) AND predicate=:p "
                "AND object_value = CAST(:o AS jsonb) AND status='active' LIMIT 1",
                {"s": sid, "p": f["predicate"], "o": json.dumps(f["object_value"], default=str)})
            if exists:
                continue
            self.db.insert_returning_id("knowledge.facts", {
                "claim_id": None, "subject_id": sid, "predicate": f["predicate"],
                "object_value": json.dumps(f["object_value"], ensure_ascii=False, default=str),
                "status": "active", "trust_tier": f["trust_tier"], "visibility": visibility,
                "tenant_id": tenant_id, "created_by": "agent",
                "verification_status": "machine_checked",
                "datatype": "ref" if "external_id" in f["object_value"] else _datatype(f["object_value"]),
                **({"ontology_version": ontology_version[:50]} if ontology_version else {}),
                "source_ref": json.dumps(f["source_ref"], ensure_ascii=False)})
            n += 1
        return {"entities_upserted": len(ids), "facts_inserted": n}
