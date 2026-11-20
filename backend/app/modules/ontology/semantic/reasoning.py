"""轻量语义推理（V1.3 语义层方案 · 能力 4 · 务实范围）。

第一阶段只做四类确定性推理，明确**不做**：完整 OWL 推理机、复杂模态/时序
逻辑、大规模物化视图自动维护。所有推理产物都是「衍生候选」（derived=true），
调用方决定是否单独持久化，绝不回写/覆盖权威关系与 Fact。

纯函数：输入 schema dict + 断言边列表，输出衍生边；带环保护与深度上限。
"""
from __future__ import annotations

from .constraints import ancestors, is_subtype_of


# ---------------------------------------------------------------- 类型继承
def descendants(schema: dict, parent_type: str) -> list[str]:
    """返回 parent_type 的所有后代类型（含自身），供「父类查询命中子类实例」。"""
    types = schema.get("entity_types") or {}
    out = []
    for name in types:
        if parent_type in ancestors(schema, name):
            out.append(name)
    return out


def instances_matching_type(schema: dict, instances: list[dict],
                            query_type: str) -> list[dict]:
    """继承感知的实例过滤：查 Person 会命中 Employee / Manager 实例。"""
    allowed = set(descendants(schema, query_type)) if query_type != "*" else None
    return [i for i in instances
            if allowed is None or (i.get("type_name") or "") in allowed]


# ---------------------------------------------------------------- 对称 / 互逆
def symmetric_completion(schema: dict, edges: list[dict]) -> list[dict]:
    """对称关系自动补全：若存在 a—R→b，补 b—R→a（去重，derived=true）。"""
    rels = schema.get("relation_types") or {}
    existing = {(str(e.get("source_id")), e.get("relation_type"),
                 str(e.get("target_id"))) for e in edges}
    out: list[dict] = []
    seen: set[tuple] = set()
    for e in edges:
        spec = rels.get(e.get("relation_type")) or {}
        chars = spec.get("characteristics") or {}
        if not chars.get("symmetric"):
            continue
        s, t, rt = str(e.get("source_id")), str(e.get("target_id")), e.get("relation_type")
        if s == t:
            continue
        key = (t, rt, s)
        if key in existing or key in seen:
            continue
        seen.add(key)
        out.append(_derived(e, source_id=t, target_id=s,
                            derived_by="symmetric", relation_type=rt))
    return out


def inverse_completion(schema: dict, edges: list[dict]) -> list[dict]:
    """互逆关系维护：a—R→b 且 R.inverse_of=S，则补 b—S→a（derived=true）。"""
    rels = schema.get("relation_types") or {}
    existing = {(str(e.get("source_id")), e.get("relation_type"),
                 str(e.get("target_id"))) for e in edges}
    out: list[dict] = []
    seen: set[tuple] = set()
    for e in edges:
        spec = rels.get(e.get("relation_type")) or {}
        inv = (spec.get("characteristics") or {}).get("inverse_of") or ""
        if not inv or inv not in rels:
            continue
        s, t = str(e.get("source_id")), str(e.get("target_id"))
        key = (t, inv, s)
        if key in existing or key in seen:
            continue
        seen.add(key)
        out.append(_derived(e, source_id=t, target_id=s,
                            derived_by="inverse", relation_type=inv))
    return out


# ---------------------------------------------------------------- 有限传递
def transitive_closure(schema: dict, edges: list[dict], relation_type: str,
                       max_depth: int | None = None) -> list[dict]:
    """单一传递关系的有限深度闭包（BFS，环保护，深度可配，默认从 Schema 读取）。

    只产出「(a, relation_type, c) 可达」的衍生边，带 depth 与 via 路径，
    供规则/决策作为确定性证据；不物化到权威关系表。
    """
    rels = schema.get("relation_types") or {}
    spec = rels.get(relation_type) or {}
    chars = spec.get("characteristics") or {}
    if not chars.get("transitive"):
        return []
    cap = int(max_depth or chars.get("max_transitive_depth")
              or (schema.get("constraints") or {}).get("max_transitive_depth") or 3)
    cap = max(1, min(cap, 6))  # 硬上限，防组合爆炸

    adj: dict[str, list[str]] = {}
    for e in edges:
        if e.get("relation_type") != relation_type or e.get("status", "active") != "active":
            continue
        adj.setdefault(str(e.get("source_id")), []).append(str(e.get("target_id")))

    asserted = {(str(e.get("source_id")), str(e.get("target_id")))
                for e in edges if e.get("relation_type") == relation_type}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for start in adj:
        # (node, depth, path) —— path 内不含 start，用于环保护与证据回溯
        frontier: list[tuple[str, int, list[str]]] = [(start, 0, [])]
        visited_at_start: set[str] = {start}
        while frontier:
            node, depth, path = frontier.pop(0)
            if depth >= cap:
                continue
            for nxt in adj.get(node, []):
                if nxt in visited_at_start:
                    continue
                visited_at_start.add(nxt)
                new_path = path + [node] if node != start else [start]
                key = (start, nxt)
                if key not in asserted and key not in seen:
                    seen.add(key)
                    out.append({
                        "source_id": start, "relation_type": relation_type,
                        "target_id": nxt, "derived": True,
                        "derived_by": "transitive", "depth": depth + 1,
                        "via": new_path + [nxt],
                        "status": "candidate",
                    })
                if depth + 1 < cap:
                    frontier.append((nxt, depth + 1, new_path))
    return out


def expand(schema: dict, edges: list[dict], max_depth: int | None = None) -> dict:
    """一次跑齐全部第一阶段推理，返回结构化结果（供 API / 决策消费）。"""
    rels = schema.get("relation_types") or {}
    trans_types = [n for n, s in rels.items()
                   if (s.get("characteristics") or {}).get("transitive")]
    transitive: list[dict] = []
    for rt in trans_types:
        transitive += transitive_closure(schema, edges, rt, max_depth=max_depth)
    symmetric = symmetric_completion(schema, edges)
    inverse = inverse_completion(schema, edges)
    return {
        "asserted_count": len(edges),
        "symmetric": symmetric,
        "inverse": inverse,
        "transitive": transitive,
        "derived_total": len(symmetric) + len(inverse) + len(transitive),
        "note": "全部为衍生候选（derived=true），未回写权威关系或 Fact",
    }


def _derived(origin: dict, *, source_id: str, target_id: str,
             derived_by: str, relation_type: str) -> dict:
    return {
        "source_id": source_id, "relation_type": relation_type,
        "target_id": target_id, "source_type": origin.get("target_type"),
        "target_type": origin.get("source_type"),
        "derived": True, "derived_by": derived_by,
        "derived_from": str(origin.get("id") or origin.get("source_id") or ""),
        "status": "candidate",
        "tenant_id": origin.get("tenant_id", "default"),
        "space_id": origin.get("space_id", "default"),
        "ontology_version": origin.get("ontology_version", "v1.0.0"),
    }
