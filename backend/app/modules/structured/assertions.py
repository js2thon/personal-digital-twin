"""确定性断言：把结构化行翻译成断言列表（纯函数、零 LLM、零 DB）。

这是整个旁路的核心。约定：

* 同样的 (契约版本, 行) 输入永远产出完全相同的断言，无随机、无时钟、无外部调用；
* `synced_at` 由调用方注入且**不参与 idempotency_key**，否则重复同步永远不幂等；
* 所有异常在写库**之前**判定：schema_drift / missing_required / enum_invalid /
  multi_manager / cycle_detected / restricted_skipped。单值维度出现多个值必须拦截，
  否则 `write_fact` 会把它当作「换了个新值」静默取代，丢掉一条真实关系。
* 断言不直接落库，由 service.py 编排调用 `ontology.write_fact` —— 依赖方向保持单向。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

from app.modules.ontology.service import cardinality_of
from app.modules.structured.contracts import VIS_RESTRICTED


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def _canon(value):
    """把任意值规范成可哈希、可比较的稳定形式。"""
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _sha(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def row_hash(row: dict) -> str:
    """源行指纹：字段顺序无关，值变化必然变化。"""
    return _sha(json.dumps(_canon(row), ensure_ascii=False, sort_keys=True, default=str))


def idempotency_key(*, contract: str, version: int, table: str, pk: str,
                    predicate: str, object_value: dict, valid_from: str) -> str:
    """幂等键不含 batch_id / synced_at：值不变时重复同步必须落在同一条断言上。"""
    return _sha("|".join([
        str(contract), str(version), str(table), str(pk), str(predicate),
        json.dumps(_canon(object_value), ensure_ascii=False, sort_keys=True),
        str(valid_from or ""),
    ]))


def _columns(rows: list[dict]) -> list[str]:
    keys: set[str] = set()
    for row in rows or []:
        if isinstance(row, dict):
            keys.update(str(k) for k in row.keys())
    return sorted(keys)


def _find_cycles(edges: dict[str, str]) -> list[list[str]]:
    """返回汇报图中的环路径（确定性：按起点排序遍历）。

    自环（pk == 主管工号）不在这里判定：HR 源里「部门负责人自己也挂在该部门下」
    是常态，主管字段填的就是本人。自环由调用方单独记 self_reference 并只丢弃
    那一条关系，不能升级成「整份文件汇报链不自洽」。
    """
    cycles: list[list[str]] = []
    for start in sorted(edges):
        if edges.get(start) == start:
            continue
        path: list[str] = []
        pos: dict[str, int] = {}
        node = start
        while node in edges and node not in pos:
            pos[node] = len(path)
            path.append(node)
            node = edges[node]
        if node in pos:
            cycle = path[pos[node]:] + [node]
            key = tuple(sorted(cycle[:-1]))
            if not any(tuple(sorted(c[:-1])) == key for c in cycles):
                cycles.append(cycle)
    return cycles


def cross_batch_anomalies(*, mapping: dict, rows: list[dict]) -> list[dict]:
    """整文件一致性预检：单值维度多值、汇报链成环。

    分批会切断「同一批内」的判定范围（两行落在不同批就查不出来），所以大文件在
    拆批**之前**先做一次轻量全量预检。这里只读映射声明的字段、不生成候选断言，
    成本与行数线性相关，不引入额外内存结构。
    """
    entity_cfg = mapping.get("entity") or {}
    pk_col = str(entity_cfg.get("pk_column") or "")
    fields = [f for f in (mapping.get("fields") or []) if isinstance(f, dict)]
    restricted = {str(c) for c in (mapping.get("sensitivity") or {})
                  .get("restricted_columns", [])}
    single = [f for f in fields
              if cardinality_of(str(f.get("predicate") or "")) == "single"
              and f.get("column") not in restricted]
    rel = [f for f in fields
           if f.get("relation_type") and f.get("object_kind") == "entity_ref"]

    by_single: dict[tuple[str, str], set[str]] = {}
    edges: dict[str, str] = {}
    self_ref: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        pk = _text(row.get(pk_col))
        if not pk:
            continue
        for f in single:
            value = _text(row.get(f["column"]))
            if value:
                by_single.setdefault((pk, str(f["predicate"])), set()).add(
                    json.dumps(_canon({"value": value}), ensure_ascii=False, sort_keys=True))
        for f in rel:
            value = _text(row.get(f["column"]))
            if not value:
                continue
            if value == pk:
                self_ref.append(pk)     # 主管填成本人：不是环，不进环检测
            else:
                edges[pk] = value

    anomalies: list[dict] = []
    for (pk, pred), vals in sorted(by_single.items()):
        if len(vals) > 1:
            anomalies.append({
                "reason": "multi_manager", "pk": pk, "field": pred,
                "detail": {"distinct_values": len(vals), "scope": "whole_file",
                           "note": "单值维度在整份文件出现多个值，已阻止写入"},
            })
    if self_ref:
        anomalies.append({"reason": "self_reference", "pk": ",".join(sorted(set(self_ref))),
                          "field": "reports_to",
                          "detail": {"count": len(set(self_ref)),
                                     "pks": sorted(set(self_ref))[:50],
                                     "scope": "whole_file",
                                     "note": ("源行主管工号等于本人工号，不建自环边；"
                                              "不阻断同文件其他人的汇报关系")}})
    cycles = _find_cycles(edges)
    if cycles:
        anomalies.append({"reason": "cycle_detected", "pk": "", "field": "reports_to",
                          "detail": {"cycles": cycles, "scope": "whole_file"}})
    return anomalies


def blocked_by(anomalies: list[dict]) -> tuple[set[tuple[str, str]], bool]:
    """把整文件预检结论翻译成「禁止写入项」：冲突维度 + 是否需要丢弃关系边。"""
    blocked = {(str(a.get("pk")), str(a.get("field")))
               for a in anomalies if a.get("reason") == "multi_manager"}
    cycles = any(a.get("reason") == "cycle_detected" for a in anomalies)
    return blocked, cycles


def build_assertions(*, mapping: dict, rows: list[dict], contract_name: str,
                     version: int, contract_id: str, contract_hash: str,
                     source_ref: dict, batch_id: str, synced_at: str = "") -> dict:
    """纯函数：契约 + 源行 → 断言 / 异常 / 统计。

    返回结构：
      {assertions: [...], anomalies: [...], stats: {...}, columns: [...], schema_ok: bool}
    """
    entity_cfg = mapping.get("entity") or {}
    fields = mapping.get("fields") or []
    sensitivity = mapping.get("sensitivity") or {}
    restricted = {str(c) for c in sensitivity.get("restricted_columns", [])}
    allowed_values = mapping.get("allowed_values") or {}
    pk_col = str(entity_cfg.get("pk_column") or "")
    name_col = str(entity_cfg.get("name_column") or "")
    prefix = str(entity_cfg.get("external_id_prefix") or "hr:")
    entity_type = str(entity_cfg.get("entity_type") or "Person")
    table = str(source_ref.get("table") or "")
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    columns = _columns(rows)
    expect = [str(c) for c in (mapping.get("expect_columns") or [])]
    anomalies: list[dict] = []
    stats = {"rows": len(rows), "assertions": 0, "restricted_skipped": 0,
             "by_predicate": {}, "schema_ok": True}

    # 1) schema 漂移：整批拒绝，绝不「能映射多少算多少」静默丢列。
    if expect and columns and sorted(expect) != columns:
        stats["schema_ok"] = False
        anomalies.append({
            "reason": "schema_drift", "pk": "", "field": "",
            "detail": {"expected": sorted(expect), "actual": columns,
                       "added": [c for c in columns if c not in expect],
                       "missing": [c for c in expect if c not in columns]},
        })
        return {"assertions": [], "anomalies": anomalies,
                "stats": stats, "columns": columns, "schema_ok": False}

    # 2) 逐行生成候选断言（先不写库，后面统一做跨行一致性检查）
    candidates: list[dict] = []
    for row in rows:
        pk = _text(row.get(pk_col))
        if not pk:
            anomalies.append({"reason": "missing_required", "pk": "",
                              "field": pk_col, "detail": {"row": _canon(row)}})
            continue
        external_id = f"{prefix}{pk}"
        canonical_name = _text(row.get(name_col)) or pk
        missing: list[str] = []
        for f in fields:
            if f.get("required") and not _text(row.get(f["column"])):
                missing.append(f["column"])
        if missing:
            anomalies.append({"reason": "missing_required", "pk": pk,
                              "field": ",".join(missing), "detail": {"row_keys": sorted(row)}})
            continue

        for f in fields:
            col = f["column"]
            raw = row.get(col)
            value = _text(raw)
            if not value:
                continue
            if col in restricted or f.get("visibility") == VIS_RESTRICTED:
                stats["restricted_skipped"] += 1
                continue
            allowed = allowed_values.get(col) or allowed_values.get(f["predicate"])
            if allowed and value not in [str(v) for v in allowed]:
                anomalies.append({"reason": "enum_invalid", "pk": pk, "field": col,
                                  "detail": {"value": value, "allowed": allowed}})
                continue
            if raw is not None and not isinstance(raw, (str, int, float, bool, datetime, date)):
                value = json.dumps(_canon(raw), ensure_ascii=False, sort_keys=True)

            if f.get("object_kind") == "entity_ref":
                target_prefix = str(f.get("target_pk_prefix") or prefix)
                target_id = f"{target_prefix}{value}"
                target_name = _text(row.get(f.get("target_name_column") or "")) or value
                obj = {"name": target_name, "external_id": target_id}
                obj_ref = {"external_id": target_id,
                           "pk_value": value,
                           "entity_type": f.get("target_entity_type") or "Person",
                           "canonical_name": target_name}
            else:
                obj = {"value": value}
                obj_ref = None

            vf = _text(row.get(f["valid_from_column"])) if f.get("valid_from_column") else ""
            vt = _text(row.get(f["valid_to_column"])) if f.get("valid_to_column") else ""
            source = {
                "channel": "structured_sync",
                "system": str(source_ref.get("system") or ""),
                "table": table,
                "pk": pk,
                "batch_id": batch_id,
                "row_hash": row_hash(row),
                "mapping_version": version,
                "contract_id": contract_id,
                "contract_hash": contract_hash,
                "synced_at": synced_at,
            }
            # 文件接入：六要素之外补文件指纹，便于回源核对「这份事实来自哪次上传」。
            for fk in ("file_name", "file_format", "file_sha256", "upload_id"):
                if source_ref.get(fk):
                    source[fk] = str(source_ref[fk])
            key = idempotency_key(contract=contract_name, version=version, table=table,
                                  pk=pk, predicate=f["predicate"], object_value=obj,
                                  valid_from=vf)
            candidates.append({
                "kind": "fact",
                "idempotency_key": key,
                "pk": pk,
                "entity": {"external_id": external_id, "entity_type": entity_type,
                           "canonical_name": canonical_name,
                           "aliases": sorted({canonical_name, _text(row.get(name_col)) or pk})},
                "predicate": f["predicate"],
                "object_value": obj,
                "object_ref": obj_ref,
                "relation_type": f.get("relation_type") or "",
                "valid_from": vf or None,
                "valid_to": vt or None,
                "visibility": f.get("visibility") or "owner",
                "confidence": 1.0,
                "source_ref": {**source, "idempotency_key": key},
            })

    # 3) 单值维度的多值冲突：接入层拦截，不能交给 write_fact 静默取代。
    by_single: dict[tuple[str, str], set[str]] = {}
    for a in candidates:
        if cardinality_of(a["predicate"]) == "single":
            by_single.setdefault((a["pk"], a["predicate"]), set()).add(
                json.dumps(_canon(a["object_value"]), ensure_ascii=False, sort_keys=True))
    blocked: set[tuple[str, str]] = set()
    for (pk, pred), vals in sorted(by_single.items()):
        if len(vals) > 1:
            blocked.add((pk, pred))
            anomalies.append({"reason": "multi_manager", "pk": pk, "field": pred,
                              "detail": {"distinct_values": len(vals),
                                         "note": "单值维度同批次出现多个值，已阻止写入"}})

    # 4) 自环：源行把主管工号填成本人工号（部门负责人自己挂在部门下是常态）。
    #    这不是环，只是「本人即本条链的顶端」。只丢这一行自己的关系断言，
    #    否则一个负责人的自指会把整份文件的汇报关系全部拖下水。
    all_edges = {a["pk"]: a["object_ref"]["pk_value"]
                 for a in candidates
                 if a["relation_type"] and a["object_ref"]}
    self_ref = sorted(pk for pk, mgr in all_edges.items() if pk and pk == mgr)
    if self_ref:
        anomalies.append({"reason": "self_reference", "pk": ",".join(self_ref),
                          "field": "reports_to",
                          "detail": {"count": len(self_ref), "pks": self_ref[:50],
                                     "note": ("源行主管工号等于本人工号，不建自环边；"
                                              "同一文件里其他人的汇报关系照常写入")}})
        drop = set(self_ref)
        candidates = [a for a in candidates
                      if not (a["relation_type"] and a["pk"] in drop)]

    # 5) 真环（≥2 个节点互相汇报）：整批关系不落库，避免写入不自洽的图。
    edges = {pk: mgr for pk, mgr in all_edges.items() if pk and pk != mgr}
    cycles = _find_cycles(edges)
    if cycles:
        anomalies.append({"reason": "cycle_detected", "pk": "", "field": "reports_to",
                          "detail": {"cycles": cycles}})
        candidates = [a for a in candidates if not a["relation_type"]]

    assertions = [a for a in candidates
                  if (a["pk"], a["predicate"]) not in blocked]
    assertions.sort(key=lambda a: (a["pk"], a["predicate"], a["idempotency_key"]))
    for a in assertions:
        stats["by_predicate"][a["predicate"]] = stats["by_predicate"].get(a["predicate"], 0) + 1
    stats["assertions"] = len(assertions)

    if stats["restricted_skipped"]:
        anomalies.append({"reason": "restricted_skipped", "pk": "", "field": "",
                          "detail": {"count": stats["restricted_skipped"],
                                     "note": "敏感字段按契约跳过，未产生任何事实"}})
    return {"assertions": assertions, "anomalies": anomalies,
            "stats": stats, "columns": columns, "schema_ok": True}
