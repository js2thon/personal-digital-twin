"""本体建模工作流 · 执行引擎（dry-run 推演 + 验证门禁）。

建模即流程：把画布 7 类建模设计节点按连线做拓扑排序，逐节点推演输入/输出与配置状态。
- dry_run：只做配置推演，不真正连库、不发 HTTP、不调模型（如实标注）；
- run：调用 validator 跑四类检查；发布门禁按四类完整验证报告判定（不再依赖画布节点）；
- 幂等：重复运行同一 spec 得到相同结构结论。

字段→属性映射内化到属性/类型节点；实例化/验证/发布在收尾区（spec.instantiate / spec.publish）。

全部纯函数，零 I/O。
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from .node_schema import NODE_TYPES, WORKFLOW_ORDER, kind_of
from .validator import PASS, FAIL, NODATA, WARN, validate_workflow


def normalize(spec: dict[str, Any]) -> dict[str, Any]:
    """归一 spec：节点 kind 统一；补标准 kind 的 order。"""
    nodes = []
    for n in spec.get("nodes") or []:
        k = kind_of(n.get("kind"))
        nodes.append({**n, "kind": k,
                      "order": NODE_TYPES.get(k, {}).get("order", 99)})
    return {**spec, "nodes": nodes, "edges": list(spec.get("edges") or [])}


def topo_order(spec: dict[str, Any]) -> dict[str, Any]:
    """Kahn 拓扑排序；返回 order（id 列表）、has_cycle、layers（分层）。"""
    ns = normalize(spec)
    nodes = ns["nodes"]
    ids = [n["id"] for n in nodes]
    idset = set(ids)
    indeg: dict[str, int] = {i: 0 for i in ids}
    adj: dict[str, list[str]] = defaultdict(list)
    for e in ns["edges"]:
        a, b = e.get("from"), e.get("to")
        if a in idset and b in idset:
            adj[a].append(b)
            indeg[b] += 1
    q = deque([i for i in ids if indeg[i] == 0])
    order: list[str] = []
    while q:
        cur = q.popleft()
        order.append(cur)
        for nx in adj[cur]:
            indeg[nx] -= 1
            if indeg[nx] == 0:
                q.append(nx)
    has_cycle = len(order) != len(ids)
    if has_cycle:
        order += [i for i in ids if i not in set(order)]

    by_id = {n["id"]: n for n in nodes}
    layers: list[list[str]] = []
    depth: dict[str, int] = {}
    for nid in order:
        deps = [e["from"] for e in ns["edges"] if e.get("to") == nid and e.get("from") in idset]
        depth[nid] = (max((depth.get(d, 0) for d in deps), default=-1) + 1)
    if layers or depth:
        maxd = max(depth.values(), default=0)
        layers = [[nid for nid in order if depth.get(nid) == d] for d in range(maxd + 1)]
    return {"order": order, "has_cycle": has_cycle, "layers": layers,
            "by_id": by_id, "node_count": len(nodes)}


def _summary_of(n: dict[str, Any]) -> str:
    f = n.get("fields") or {}
    k = n.get("kind")
    if k == "datasource":
        st = str(f.get("source_type") or "file")
        tgt = f.get("file_name") or f.get("api_url") or f.get("db_table") or f.get("source") or ""
        return f"{st}" + (f" · {tgt}" if tgt else "")
    if k == "entity":
        return f"PK={f.get('primary_key') or '未声明'}"
    if k == "relation":
        return f"{f.get('source_entity') or '?'} →[{f.get('cardinality') or '?'}]→ {f.get('target_entity') or '?'}"
    if k == "function":
        return f"{f.get('inputs') or ''} → {f.get('output') or '?'}"
    if k == "validation":
        return "四类检查强制门禁"
    if k == "publish":
        return f"版本 {f.get('version') or '?'} · 门禁={'开' if f.get('gate_validation', True) else '关'}"
    return str(f.get("name") or f.get("output") or f.get("effect") or "")[:40]


def dry_run(spec: dict[str, Any]) -> dict[str, Any]:
    """逐节点配置推演。不连接任何数据源、不发请求、不调模型。"""
    ns = normalize(spec)
    topo = topo_order(spec)
    by_id = topo["by_id"]
    edges = ns["edges"]
    steps = []
    for nid in topo["order"]:
        n = by_id[nid]
        k = n.get("kind")
        meta = NODE_TYPES.get(k or "", {})
        ins = [e["from"] for e in edges if e.get("to") == nid]
        outs = [e["to"] for e in edges if e.get("from") == nid]
        steps.append({
            "id": nid, "kind": k, "label": meta.get("label", k or "未知"),
            "name": (n.get("fields") or {}).get("name") or "",
            "layer": meta.get("layer", "?"),
            "summary": _summary_of(n),
            "inputs": [by_id[i].get("fields", {}).get("name") or i for i in ins if i in by_id],
            "outputs": [by_id[o].get("fields", {}).get("name") or o for o in outs if o in by_id],
        })
    return {
        "mode": "dry-run",
        "note": "配置推演：不真正连接数据源、不发 HTTP 请求、不调用模型。质量结论请用 run（完整验证）。",
        "has_cycle": topo["has_cycle"],
        "steps": steps,
    }


def run(spec: dict[str, Any], data: dict[str, Any] | None = None) -> dict[str, Any]:
    """运行完整验证；发布门禁按四类验证报告判定（不依赖画布上的 publish 节点）。

    发布版本与实例化配置来自收尾区（spec.publish / spec.instantiate），随请求传入；
    只要四类完整验证报告未全过，即阻断发布——报告即门禁依据。
    """
    report = validate_workflow(spec, data)
    ns = normalize(spec)
    # 收尾区发布配置（旧 spec 内若有 publish 节点，也读取其字段兼容）
    pub_cfg = dict(spec.get("publish") or {})
    legacy_pubs = [n for n in ns["nodes"] if n.get("kind") == "publish"]
    for n in legacy_pubs:
        f = n.get("fields") or {}
        for k in ("version", "gate_validation", "channel"):
            if k not in pub_cfg and k in f:
                pub_cfg[k] = f[k]
    version = str(pub_cfg.get("version") or "").strip()
    gate_on = pub_cfg.get("gate_validation", True) is not False
    has_target = bool(version)  # 收尾区填了版本号才算有发布目标
    blocked = bool(gate_on and has_target and not report["complete"])
    return {
        "mode": data.get("mode", "none") if isinstance(data, dict) else "none",
        "report": report,
        "publish": {
            "allowed": (report["complete"] or not gate_on) if has_target else False,
            "blocked": blocked,
            "reason": ("未通过四类完整验证，发布被门禁阻断" if blocked else
                       ("验证通过，允许发布 " + version if has_target and report["complete"] else
                        ("验证通过但未填版本号，不发起发布" if report["complete"] else
                         "未设发布版本 / 未开启门禁"))),
            "versions": [version] if version else [],
            "source": "tail" if spec.get("publish") else "legacy-node",
        },
        "instantiate": dict(spec.get("instantiate") or {}),
        "dry_run": dry_run(spec),
    }


def self_check() -> dict[str, Any]:
    r = run({"version": "1.0", "nodes": [], "edges": []})
    # 空草稿：无收尾区发布版本 → 门禁不阻断（无发布目标），但验证未完成
    ok = r["publish"]["blocked"] is False and r["report"]["complete"] is False
    return {"pass": ok, "note": "空草稿无发布目标：不阻断，但未完成"}
