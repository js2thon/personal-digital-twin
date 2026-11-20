"""影响分析（V1.3 语义层方案 · P0-2 · 文章《你的AI理解不了你的业务》落地）。

回答文章里的核心问题：「某业务对象变更，会沿关系波及谁？」
——例如「李四调岗，影响哪些下属、哪些部门、哪些订单」。

设计铁律（沿用 semantic 包）：
* **纯函数**：只吃 nodes / edges 内存结构，无 DB / Web 依赖，离线可测；
  取数与装配由 API 层负责（store → 这里）。
* **只读**：产出「受影响对象 + 影响路径 + 可信度」，绝不改写权威事实/关系。
* **方向感知**：关系类型可声明影响传播方向（reports_to 向上/向下、works_at 双向），
  不声明则默认双向；每条命中都带完整路径，可解释、可评估。
* **照实披露**：无标注集时 `path_query_accuracy.available=false`，绝不编造准确率。

影响传播方向：
  downstream : 源变更影响其「下级/成员」（沿 source→target 正向走，如 manager→reports）
  upstream   : 源变更影响其「上级/归属」（沿 target→source 反向走，如 reports→manager）
  both       : 双向（默认）
"""
from __future__ import annotations

from collections import deque
from typing import Any

# 关系默认传播方向；与 schema_def 的 characteristics 语义保持一致，但不耦合。
DEFAULT_DIRECTION: dict[str, str] = {
    "reports_to": "both",          # 上级变动影响下级，下级变动也影响上级的团队结构
    "member_of": "both",
    "works_at": "both",
    "collaborates_with": "both",
    "is_project_manager_of": "downstream",
    "managed_by": "upstream",
    "belongs_to_department": "both",
}

DIRECTIONS = {"downstream", "upstream", "both"}


def normalize_direction(raw: Any) -> str:
    return raw if raw in DIRECTIONS else "both"


def _adjacency(edges: list[dict], directions: dict[str, str]) -> dict[str, list[tuple[str, str, int]]]:
    """构造邻接表：node → [(邻居, 关系类型, 方向符号)]，方向符号 +1 正向 / -1 反向。"""
    adj: dict[str, list[tuple[str, str, int]]] = {}
    for e in edges or []:
        s = str(e.get("source_id") or e.get("source") or "")
        t = str(e.get("target_id") or e.get("target") or "")
        rtype = str(e.get("relation_type") or e.get("predicate") or "")
        if not s or not t or s == t:
            continue  # 自环无影响意义
        d = normalize_direction(directions.get(rtype))
        # downstream: 只走 s→t；upstream: 只走 t→s；both: 双向
        if d in ("downstream", "both"):
            adj.setdefault(s, []).append((t, rtype, 1))
        if d in ("upstream", "both"):
            adj.setdefault(t, []).append((s, rtype, -1))
    return adj


def analyze_impact(
    root_id: str,
    nodes: list[dict],
    edges: list[dict],
    *,
    max_depth: int = 3,
    directions: dict[str, str] | None = None,
    relation_types: list[str] | None = None,
    max_nodes: int = 200,
) -> dict:
    """多跳影响分析：从 root 出发沿关系追溯受影响对象。

    返回：
      root       : 起点节点
      affected   : [{id,name,type,depth,path:[...],via:[关系...]}]，按 depth 升序
      by_depth   : {1:n,2:n,...}
      relation_hits: {关系类型: 命中数}
      truncated  : 是否因 max_nodes 截断
      path_query_accuracy: {available:false, reason:...}（无标注集，照实披露）
    """
    dirs = {**(DEFAULT_DIRECTION), **(directions or {})}
    try:
        depth_cap = max(1, min(int(max_depth), 6))
    except (TypeError, ValueError):
        depth_cap = 3
    try:
        node_cap = max(1, min(int(max_nodes), 2000))
    except (TypeError, ValueError):
        node_cap = 200

    name_by_id = {
        str(n.get("id")): {
            "name": n.get("canonical_name") or n.get("name") or str(n.get("id")),
            "type": n.get("entity_type") or n.get("type") or "",
        }
        for n in nodes or []
    }
    allowed = set(relation_types) if relation_types else None

    # 构造邻接（按关系类型过滤）
    filtered_edges = [e for e in (edges or []) if not allowed or
                      str(e.get("relation_type") or e.get("predicate") or "") in allowed]
    adj = _adjacency(filtered_edges, dirs)

    root = str(root_id)
    seen: dict[str, dict] = {root: {"depth": 0, "path": [root], "via": []}}
    queue = deque([root])
    truncated = False
    while queue:
        cur = queue.popleft()
        info = seen[cur]
        if info["depth"] >= depth_cap:
            continue
        for nb, rtype, sign in adj.get(cur, []):
            if nb in seen:
                continue
            if len(seen) >= node_cap:
                truncated = True
                break
            seen[nb] = {
                "depth": info["depth"] + 1,
                "path": info["path"] + [nb],
                "via": info["via"] + [f"{rtype}{'↑' if sign < 0 else '↓'}"],
            }
            queue.append(nb)

    affected = []
    by_depth: dict[str, int] = {}
    relation_hits: dict[str, int] = {}
    for nid, info in seen.items():
        if nid == root:
            continue
        meta = name_by_id.get(nid, {"name": nid, "type": ""})
        affected.append({
            "id": nid, "name": meta["name"], "type": meta["type"],
            "depth": info["depth"],
            "path": [name_by_id.get(p, {"name": p}).get("name", p) for p in info["path"]],
            "via": info["via"],
        })
        by_depth[str(info["depth"])] = by_depth.get(str(info["depth"]), 0) + 1
        for v in info["via"]:
            rt = v[:-1]  # 去掉方向符号
            relation_hits[rt] = relation_hits.get(rt, 0) + 1
    affected.sort(key=lambda x: (x["depth"], x["name"]))

    return {
        "root": {"id": root, **name_by_id.get(root, {"name": root, "type": ""})},
        "affected": affected,
        "total": len(affected),
        "by_depth": by_depth,
        "relation_hits": relation_hits,
        "truncated": truncated,
        "params": {"max_depth": depth_cap, "max_nodes": node_cap,
                   "relation_types": sorted(allowed) if allowed else None,
                   "directions": dirs},
        "path_query_accuracy": {
            "available": False,
            "reason": "无标注集：影响路径为确定性图遍历结果，未与人工标注比对，不给出准确率数值",
        },
    }


def self_check() -> dict:
    """内置可评估小样本（3 节点汇报链），验证遍历与方向正确，供回归测试用。"""
    nodes = [
        {"id": "a", "canonical_name": "李四", "entity_type": "Person"},
        {"id": "b", "canonical_name": "赵六", "entity_type": "Person"},
        {"id": "c", "canonical_name": "王五", "entity_type": "Person"},
    ]
    edges = [
        {"source_id": "b", "target_id": "a", "relation_type": "reports_to"},  # b 汇报 a
        {"source_id": "c", "target_id": "b", "relation_type": "reports_to"},  # c 汇报 b
    ]
    r = analyze_impact("a", nodes, edges, max_depth=3)
    names = {x["name"] for x in r["affected"]}
    return {
        "pass": (r["total"] == 2 and "赵六" in names and "王五" in names),
        "total": r["total"], "names": sorted(names),
        "note": "a(李四) 的影响应波及 b(赵六,1跳) 与 c(王五,2跳)",
    }
