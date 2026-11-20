"""知识图谱内核：关系类型目录 + k 跳子图 + 路径 + 质量统计。

设计边界（与蓝图一致）：
- 图在 PostgreSQL 上实现，蓝图第十五节冻结 Neo4j；本模块**只读**，不写库、不改任何事实。
- 关系类型是「已登记目录」，不是写入白名单：未登记类型照样能查，但会在统计里单列，
  用于暴露抽取噪声（如历史数据里的 `will_know_after_signing`）。这样既不阻断存量，
  也不假装所有关系都合规。
- 全部 SQL 只用绑定参数；关系类型筛选走 JSON 数组参数，避免拼接字符串。
- 环保护：递归 CTE 用 path 数组记录已访问节点，`NOT (next = ANY(path))` 截断，
  因此环路不会把查询打爆（本体自检里的 cycle_detected 是写入侧的另一道闸门）。

纯函数（可离线测）：`classify` / `clamp_depth` / `parse_types` / `assemble_graph` /
`components` / `shape_stats`。
"""
import json

# ---------------------------------------------------------------- 关系类型目录
# source / target 是「应当」的类型约束，用于前端提示与将来的写入校验；
# "*" 表示不限。cardinality="one" 表示单值维度（同一主体只应有一条 active 边）。

# 部门类实体：本体 Department 是等价类（Department ⊑ OrganizationalUnit），
# 实例化时 entity_type 会落到具体层级子类（DepartmentLevel1..7）。
# 关系类型约束必须按「类集合」匹配，否则同一条边在不同层级部门之间会被误判类型越界。
DEPT_TYPES: list[str] = ["Department", "OrganizationalUnit"] + [
    f"DepartmentLevel{i}" for i in range(1, 8)
]

RELATION_TYPES: dict[str, dict] = {
    # 组织
    "reports_to": {"label": "汇报给", "family": "org", "source": ["Person"],
                   "target": ["Person"], "cardinality": "one"},
    "member_of": {"label": "隶属于", "family": "org", "source": ["Person"],
                  "target": ["Organization"], "cardinality": "one"},
    "belongs_to_organization": {"label": "所属组织", "family": "org", "source": ["*"],
                                "target": ["Organization"], "cardinality": "one"},
    "belongs_to_department": {"label": "所属部门", "family": "org", "source": ["Person"],
                              "target": ["Organization"], "cardinality": "one"},
    "has_role": {"label": "担任", "family": "org", "source": ["Person"],
                 "target": ["Role"], "cardinality": "many"},
    "is_project_manager_of": {"label": "是项目负责人", "family": "org",
                              "source": ["Person"], "target": ["Project"], "cardinality": "many"},
    "manages": {"label": "管理", "family": "org",
                "source": ["Person", "Employee"],
                "target": ["Person", "Employee"], "cardinality": "many"},
    "located_in": {"label": "位于", "family": "org", "source": ["*"],
                   "target": ["Organization"], "cardinality": "many"},
    # 人员基本信息本体 v1.0.0 的 6 个 owl:ObjectProperty（camelCase 与本体 IRI 一致）
    # 这 6 个谓词由「本体建模 → 实例化」直接落库，与抽取类关系并存但语义来源不同：
    # 前者受 TTL 的 domain/range 约束，后者来自自然语言抽取。
    "belongsToDepartment": {"label": "所属部门", "family": "org",
                            "source": ["Employee"], "target": DEPT_TYPES,
                            "cardinality": "one"},
    "hasLineManager": {"label": "直属上级", "family": "org",
                       "source": ["Employee"], "target": ["Employee"],
                       "cardinality": "one"},
    "hasDepartmentManager": {"label": "部门负责人", "family": "org",
                             "source": DEPT_TYPES, "target": ["Employee"],
                             "cardinality": "one"},
    # owl:inverseOf :hasDepartmentManager —— 方向是「人 → 部门」，不要写成和正向外延一样
    "isManagerOfDepartment": {"label": "是部门负责人", "family": "org",
                              "source": ["Employee"], "target": DEPT_TYPES,
                              "cardinality": "many"},
    "hasParentDepartment": {"label": "上级部门", "family": "org",
                            "source": DEPT_TYPES, "target": DEPT_TYPES,
                            "cardinality": "one"},
    # 工作与项目
    "works_on": {"label": "参与", "family": "work", "source": ["Person"],
                 "target": ["Project"], "cardinality": "many"},
    "works_at": {"label": "就职于", "family": "work", "source": ["Person"],
                 "target": ["Organization"], "cardinality": "one"},
    "participated_in": {"label": "参与过", "family": "work", "source": ["Person"],
                        "target": ["Project", "Event"], "cardinality": "many"},
    "completed": {"label": "完成", "family": "work", "source": ["Person"],
                  "target": ["Project"], "cardinality": "many"},
    "is_owner_of": {"label": "负责", "family": "work", "source": ["Person"],
                    "target": ["Project", "Document"], "cardinality": "many"},
    "owns": {"label": "拥有", "family": "work", "source": ["*"], "target": ["*"],
             "cardinality": "many"},
    "announced": {"label": "发布", "family": "work", "source": ["Person"],
                  "target": ["Document", "Event", "Project"], "cardinality": "many"},
    "assigned_to": {"label": "指派给", "family": "work", "source": ["*"],
                    "target": ["Person"], "cardinality": "many"},
    "depends_on": {"label": "依赖", "family": "work", "source": ["Project", "System"],
                   "target": ["Project", "System"], "cardinality": "many"},
    "part_of": {"label": "属于", "family": "work", "source": ["*"], "target": ["*"],
                "cardinality": "one"},
    # 知识
    "believes": {"label": "认为", "family": "knowledge", "source": ["Person"],
                 "target": ["Concept", "Policy", "Rule"], "cardinality": "many"},
    "prefers": {"label": "偏好", "family": "knowledge", "source": ["Person"],
                "target": ["Concept", "System", "Policy"], "cardinality": "many"},
    "interested_in": {"label": "关注", "family": "knowledge", "source": ["Person"],
                      "target": ["Concept", "Domain", "Project"], "cardinality": "many"},
    "knows": {"label": "掌握", "family": "knowledge", "source": ["Person"],
              "target": ["Skill", "Concept"], "cardinality": "many"},
    "teaches": {"label": "讲授", "family": "knowledge", "source": ["Person"],
                "target": ["Person", "Concept"], "cardinality": "many"},
    "learns_from": {"label": "学习自", "family": "knowledge", "source": ["Person"],
                    "target": ["Person", "Document"], "cardinality": "many"},
    # 系统与工具
    "integrates_with": {"label": "集成", "family": "system", "source": ["System"],
                        "target": ["System"], "cardinality": "many"},
    "can_login_via": {"label": "可登录", "family": "system", "source": ["Person"],
                      "target": ["System"], "cardinality": "many"},
    "uses": {"label": "使用", "family": "system", "source": ["Person", "System"],
             "target": ["System", "Component"], "cardinality": "many"},
    "runs_on": {"label": "运行于", "family": "system", "source": ["System", "Component"],
                "target": ["System"], "cardinality": "many"},
    "replaces": {"label": "替代", "family": "system", "source": ["*"], "target": ["*"],
                 "cardinality": "many"},
    # 人际与文档
    "follows_external_experts": {"label": "关注专家", "family": "social",
                                 "source": ["Person"], "target": ["Person"],
                                 "cardinality": "many"},
    "collaborates_with": {"label": "协作", "family": "social", "source": ["Person"],
                          "target": ["Person"], "cardinality": "many"},
    "mentored_by": {"label": "受指导于", "family": "social", "source": ["Person"],
                    "target": ["Person"], "cardinality": "many"},
    "mentions": {"label": "提及", "family": "doc", "source": ["Document"], "target": ["*"],
                 "cardinality": "many"},
    "authored": {"label": "撰写", "family": "doc", "source": ["Person"],
                 "target": ["Document"], "cardinality": "many"},
    "references": {"label": "引用", "family": "doc", "source": ["Document", "Claim"],
                   "target": ["Document", "Concept"], "cardinality": "many"},
}

FAMILY_LABELS = {"org": "组织", "work": "工作", "knowledge": "认知",
                 "system": "系统", "social": "人际", "doc": "文档", "other": "未分类"}

MAX_DEPTH = 3          # 子图最多 3 跳：再多就不是「看关系」而是「看全库」
MAX_PATH_DEPTH = 4     # 路径枚举深度上限（组合爆炸保护）
MAX_NODES = 500
DEFAULT_NODES = 150


def classify(relation_type: str) -> dict:
    """关系类型 → 展示用元数据。未登记类型不报错，标记 registered=False。"""
    meta = RELATION_TYPES.get(relation_type)
    if not meta:
        return {"type": relation_type, "label": relation_type, "family": "other",
                "registered": False, "source": ["*"], "target": ["*"],
                "cardinality": "many"}
    return {"type": relation_type, "label": meta["label"], "family": meta["family"],
            "registered": True, "source": meta["source"], "target": meta["target"],
            "cardinality": meta["cardinality"]}


def relation_catalog(usage: dict[str, int] | None = None) -> list[dict]:
    """目录 + 实际用量。usage 为空时 used=0，用于「已登记但没人用」的对比。"""
    usage = usage or {}
    rows = []
    for rtype, meta in RELATION_TYPES.items():
        rows.append({"type": rtype, "label": meta["label"], "family": meta["family"],
                     "family_label": FAMILY_LABELS.get(meta["family"], "未分类"),
                     "source": meta["source"], "target": meta["target"],
                     "cardinality": meta["cardinality"],
                     "registered": True, "used": int(usage.get(rtype, 0))})
    for rtype, n in usage.items():
        if rtype in RELATION_TYPES:
            continue
        rows.append({"type": rtype, "label": rtype, "family": "other",
                     "family_label": "未登记", "source": ["*"], "target": ["*"],
                     "cardinality": "many", "registered": False, "used": int(n)})
    rows.sort(key=lambda r: (-r["used"], r["type"]))
    return rows


def clamp_depth(depth) -> int:
    try:
        d = int(depth)
    except (TypeError, ValueError):
        return 2
    return max(1, min(d, MAX_DEPTH))


def clamp_limit(limit, default: int = DEFAULT_NODES) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, MAX_NODES))


def parse_types(raw) -> list[str]:
    """'a,b' 或 ['a','b'] → 去重列表。只接受标识符字符，避免异常输入进 SQL。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.split(",")
    else:
        parts = list(raw)
    out, seen = [], set()
    for p in parts:
        s = str(p).strip()
        if not s or len(s) > 64:
            continue
        if not all(c.isalnum() or c in "_-.:" for c in s):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= 40:
            break
    return out


# ---------------------------------------------------------------- SQL 构造

def _type_filter(types: list[str]) -> str:
    """关系类型过滤片段。空列表 = 不过滤。types 以 JSON 文本参数传入。"""
    return ("(:types_json = '[]' OR r.relation_type IN "
            "(SELECT jsonb_array_elements_text(CAST(:types_json AS jsonb))))")


def _status_filter() -> str:
    return "(:status = 'any' OR r.status = :status)"


def _reach_cte() -> str:
    """以 :root 为起点的 k 跳可达集合，带环保护。"""
    return (
        "WITH RECURSIVE walk AS ("
        "  SELECT CAST(:root AS uuid) AS node_id, 0 AS depth,"
        "         ARRAY[CAST(:root AS uuid)] AS path"
        "  UNION ALL"
        "  SELECT nb.next_id, w.depth + 1, w.path || nb.next_id"
        "  FROM walk w"
        "  CROSS JOIN LATERAL ("
        "    SELECT CASE WHEN r.source_entity_id = w.node_id THEN r.target_entity_id"
        "                ELSE r.source_entity_id END AS next_id"
        "    FROM ontology.relations r"
        "    WHERE (r.source_entity_id = w.node_id OR r.target_entity_id = w.node_id)"
        "      AND " + _status_filter() + ""
        "      AND " + _type_filter([]) + ""
        "  ) nb"
        "  WHERE w.depth < :depth AND NOT (nb.next_id = ANY(w.path))"
        "), reach AS (SELECT DISTINCT node_id FROM walk) "
    )


def subgraph_queries(center: str | None, depth: int, types: list[str],
                     status: str = "active", limit: int = DEFAULT_NODES) -> dict:
    """返回 {"nodes": (sql, params), "edges": (sql, params)}。

    center 为空 → 全图（按创建时间倒序取前 limit 个实体）。
    center 有值 → 以该实体为中心做 k 跳子图。
    """
    depth = clamp_depth(depth)
    limit = clamp_limit(limit)
    params = {"types_json": json.dumps(types, ensure_ascii=False),
              "status": status or "active", "limit": limit,
              "edge_limit": min(limit * 8, 4000)}
    if center:
        params.update({"root": str(center), "depth": depth})
        nodes_sql = (_reach_cte() +
                     "SELECT e.id, e.canonical_name, e.entity_type, e.status "
                     "FROM ontology.entities e "
                     "JOIN reach ON reach.node_id = e.id "
                     "ORDER BY e.entity_type, e.canonical_name LIMIT :limit")
        edges_sql = (_reach_cte() +
                     "SELECT r.id, r.source_entity_id, r.target_entity_id, "
                     "       r.relation_type, r.status, r.evidence_claim_id, "
                     "       r.valid_from, r.valid_to "
                     "FROM ontology.relations r "
                     "JOIN reach a ON a.node_id = r.source_entity_id "
                     "JOIN reach b ON b.node_id = r.target_entity_id "
                     "WHERE " + _status_filter() + " AND " + _type_filter(types) + " "
                     "ORDER BY r.valid_from DESC NULLS LAST LIMIT :edge_limit")
        return {"nodes": (nodes_sql, params), "edges": (edges_sql, params)}

    nodes_sql = ("SELECT id, canonical_name, entity_type, status FROM ontology.entities "
                 "WHERE status IN ('active', 'candidate') "
                 "ORDER BY created_at DESC LIMIT :limit")
    edges_sql = ("SELECT r.id, r.source_entity_id, r.target_entity_id, r.relation_type, "
                 "       r.status, r.evidence_claim_id, r.valid_from, r.valid_to "
                 "FROM ontology.relations r "
                 "WHERE " + _status_filter() + " AND " + _type_filter(types) + " "
                 "  AND r.source_entity_id IN (SELECT id FROM ontology.entities "
                 "        WHERE status IN ('active', 'candidate')) "
                 "  AND r.target_entity_id IN (SELECT id FROM ontology.entities "
                 "        WHERE status IN ('active', 'candidate')) "
                 "ORDER BY r.valid_from DESC NULLS LAST LIMIT :edge_limit")
    return {"nodes": (nodes_sql, params), "edges": (edges_sql, params)}


def paths_query(source: str, target: str, max_depth: int = MAX_PATH_DEPTH) -> tuple[str, dict]:
    """枚举 source→target 的简单路径（不重复节点），按跳数升序。"""
    depth = max(1, min(int(max_depth or MAX_PATH_DEPTH), MAX_PATH_DEPTH))
    sql = (
        "WITH RECURSIVE search AS ("
        "  SELECT CAST(:src AS uuid) AS node_id, ARRAY[CAST(:src AS uuid)] AS path,"
        "         ARRAY[]::text[] AS rels, 0 AS depth"
        "  UNION ALL"
        "  SELECT nb.next_id, s.path || nb.next_id, s.rels || nb.rel_type, s.depth + 1"
        "  FROM search s"
        "  CROSS JOIN LATERAL ("
        "    SELECT CASE WHEN r.source_entity_id = s.node_id THEN r.target_entity_id"
        "                ELSE r.source_entity_id END AS next_id,"
        "           r.relation_type AS rel_type"
        "    FROM ontology.relations r"
        "    WHERE (r.source_entity_id = s.node_id OR r.target_entity_id = s.node_id)"
        "      AND r.status = 'active'"
        "  ) nb"
        "  WHERE s.depth < :depth AND NOT (nb.next_id = ANY(s.path))"
        ") "
        "SELECT path, rels, depth FROM search "
        "WHERE node_id = CAST(:dst AS uuid) ORDER BY depth LIMIT 20"
    )
    return sql, {"src": str(source), "dst": str(target), "depth": depth}


# ---------------------------------------------------------------- 组装与统计

def type_violation(meta: dict, source_type: str, target_type: str) -> str | None:
    """关系两端实体类型是否符合目录约束。返回 None 表示合规，否则返回原因。

    这是「本体能力」里最容易被忽略的一环：谓词本身没错，但 `has_role` 指向了 Project
    就说明抽取把「担任项目负责人」压成了「担任」。不阻断写入，只标注出来。
    """
    if not meta.get("registered"):
        return "unregistered_relation_type"
    if "*" not in meta["source"] and source_type not in meta["source"]:
        return f"source_type_mismatch({source_type})"
    if "*" not in meta["target"] and target_type not in meta["target"]:
        return f"target_type_mismatch({target_type})"
    return None


def assemble_graph(node_rows: list[dict], edge_rows: list[dict],
                   center: str | None = None, depth: int = 2,
                   isolated_hidden: bool = False,
                   limit: int = DEFAULT_NODES) -> dict:
    """把 SQL 行装成前端可直接渲染的图，并算度数与密度。

    只保留两端都在节点集里的边（节点被 limit 截断时不会出现悬空边）。
    """
    nodes = {}
    for r in node_rows:
        nodes[str(r["id"])] = {"id": str(r["id"]), "name": r.get("canonical_name") or "",
                               "type": r.get("entity_type") or "Unknown",
                               "status": r.get("status") or "active", "degree": 0}
    edges = []
    for r in edge_rows:
        s, t = str(r["source_entity_id"]), str(r["target_entity_id"])
        if s not in nodes or t not in nodes:
            continue
        meta = classify(r["relation_type"])
        problem = type_violation(meta, nodes[s]["type"], nodes[t]["type"])
        edges.append({"id": str(r["id"]), "source": s, "target": t,
                      "type": r["relation_type"], "label": meta["label"],
                      "family": meta["family"], "registered": meta["registered"],
                      "valid": problem is None, "problem": problem,
                      "status": r.get("status") or "active",
                      "claim_id": str(r["evidence_claim_id"]) if r.get("evidence_claim_id") else None})
        nodes[s]["degree"] += 1
        nodes[t]["degree"] += 1

    node_list = list(nodes.values())
    isolated = [n for n in node_list if n["degree"] == 0]
    if isolated_hidden:
        node_list = [n for n in node_list if n["degree"] > 0]

    n, e = len(node_list), len(edges)
    density = round(2 * e / (n * (n - 1)), 4) if n > 1 else 0.0
    type_counts: dict[str, int] = {}
    for node in node_list:
        type_counts[node["type"]] = type_counts.get(node["type"], 0) + 1
    unknown = sorted({ed["type"] for ed in edges if not ed["registered"]})
    return {
        "center": center,
        "depth": clamp_depth(depth),
        "nodes": node_list,
        "edges": edges,
        "truncated": len(node_rows) >= clamp_limit(limit),
        "stats": {"nodes": n, "edges": e, "isolated": len(isolated),
                  "density": density, "type_counts": type_counts,
                  "unknown_relation_types": unknown,
                  "violations": sum(1 for ed in edges if not ed["valid"])},
    }


def components(node_ids: list[str], edge_pairs: list[tuple[str, str]]) -> int:
    """连通分量个数（并查集）。孤立点各自算一个分量。"""
    parent = {str(n): str(n) for n in node_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s, t in edge_pairs:
        s, t = str(s), str(t)
        if s not in parent or t not in parent:
            continue
        rs, rt = find(s), find(t)
        if rs != rt:
            parent[rs] = rt
    return len({find(n) for n in parent})


def shape_stats(entity_rows: list[dict], relation_rows: list[dict]) -> dict:
    """图谱质量：规模、孤立点、未登记关系类型、连通分量、度数前五。"""
    ids = [str(r["id"]) for r in entity_rows]
    names = {str(r["id"]): (r.get("canonical_name") or "") for r in entity_rows}
    types = {str(r["id"]): (r.get("entity_type") or "Unknown") for r in entity_rows}
    active_edges = [(str(r["source_entity_id"]), str(r["target_entity_id"]),
                     r["relation_type"])
                    for r in relation_rows if (r.get("status") or "active") == "active"]
    degree = {i: 0 for i in ids}
    usage: dict[str, int] = {}
    for s, t, rtype in active_edges:
        usage[rtype] = usage.get(rtype, 0) + 1
        if s in degree:
            degree[s] += 1
        if t in degree:
            degree[t] += 1
    isolated = [i for i, d in degree.items() if d == 0]
    top = sorted(degree.items(), key=lambda kv: (-kv[1], names.get(kv[0], "")))[:5]
    violations = []
    for s, t, rtype in active_edges:
        meta = classify(rtype)
        problem = type_violation(meta, types.get(s, "Unknown"), types.get(t, "Unknown"))
        if problem:
            violations.append({"relation": rtype, "problem": problem,
                               "source": names.get(s, s), "target": names.get(t, t),
                               "source_type": types.get(s, "Unknown"),
                               "target_type": types.get(t, "Unknown")})
    return {
        "entities": len(ids),
        "relations": len(active_edges),
        "isolated": len(isolated),
        "isolated_ratio": round(len(isolated) / len(ids), 4) if ids else 0.0,
        "components": components(ids, [(s, t) for s, t, _ in active_edges]),
        "relation_types_used": len(usage),
        "unregistered_types": sorted([t for t in usage if t not in RELATION_TYPES]),
        "type_violations": len(violations),
        "violation_samples": violations[:10],
        "top_degree": [{"id": i, "name": names.get(i, ""), "type": types.get(i, ""),
                        "degree": d} for i, d in top if d > 0],
    }


def describe_paths(rows: list[dict], name_by_id: dict[str, str],
                   type_by_id: dict[str, str]) -> list[dict]:
    """路径行 → 可读链路。rel 方向按 path 相邻节点还原，便于前端标注箭头。"""
    out = []
    for r in rows:
        path = [str(p) for p in (r.get("path") or [])]
        rels = list(r.get("rels") or [])
        hops = []
        for i, rel_type in enumerate(rels):
            if i + 1 >= len(path):
                break
            meta = classify(rel_type)
            hops.append({"from": path[i], "to": path[i + 1], "type": rel_type,
                         "label": meta["label"], "registered": meta["registered"]})
        out.append({
            "depth": int(r.get("depth") or len(hops)),
            "nodes": [{"id": p, "name": name_by_id.get(p, p), "type": type_by_id.get(p, "Unknown")}
                      for p in path],
            "hops": hops,
            "text": " → ".join([name_by_id.get(path[0], path[0])] +
                               [f"-[{h['label']}]-> {name_by_id.get(h['to'], h['to'])}"
                                for h in hops]) if path else "",
        })
    return out
