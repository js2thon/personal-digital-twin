"""⑤ 基于本体自动生成指标：原子 / 派生 / 复合 + 校验 + 计算 + SQL 编译 + 血缘。

输入只依赖本体（schema，与 cq_v2 Registry 同构）——不看具体数据，所以任何数据源
自动建模后都能直接生成指标；数据只在 `evaluate` 时使用。

指标三层：
- atomic    原子：类 + 聚合 + 度量（count / sum / avg / max / count_distinct）
- derived   派生：原子 + 时间粒度（month/quarter/year）或 环比/同比
- composite 复合：指标间运算（比率 / 人均 / 占比 / 管理幅度），要求分子分母有共同维度

维度自动推导：类自身的 enum 属性 + 沿 many_to_one 关系 ≤3 跳可达类的 name/enum 属性。
"""
from __future__ import annotations

import re
from collections import deque

AGG_BY_ROLE = {"measure": ("sum", "avg", "max")}
GRAINS = ("month", "quarter", "year")
MAX_DIM_HOPS = 3
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _snake(s: str) -> str:
    """类名 → 合法 ASCII 指标前缀。中文/符号类名（如 数据管理部人员）不能直接用：
    metric_engine 要求字母/下划线开头的 ASCII 标识符。退化为 cls_<短哈希>，稳定可复现。"""
    out = re.sub(r"(?<!^)(?=[A-Z])", "_", s or "").lower()
    out = re.sub(r"[^a-z0-9_]+", "_", out).strip("_")
    out = re.sub(r"_+", "_", out)
    if not out or not re.match(r"[a-z_]", out):
        import hashlib
        out = "cls_" + hashlib.md5((s or "").encode("utf-8")).hexdigest()[:8]
    return out


_VALID_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ------------------------------------------------------------------ 维度
def dimensions(schema: dict, cls: str, include_self_name: bool = True) -> list[dict]:
    """cls 可用维度：[{id, path:[rel...], class, attribute, label}]。"""
    attrs, rels = schema["attributes"], schema["relation_types"]
    labels = {k: v.get("label") or k for k, v in schema["entity_types"].items()}
    out, seen = [], set()
    q = deque([(cls, [])])
    visited = {cls}
    while q:
        cur, path = q.popleft()
        for a, m in attrs.items():
            if m["owner"] != cur or m.get("role") not in ("enum", "name"):
                continue
            if not path and m.get("role") == "name" and not include_self_name:
                continue                         # 自身名称：仅作复合指标分母的对齐维度
            did = "__".join(path + [a])
            if did not in seen:
                seen.add(did)
                out.append({"id": did, "path": list(path), "class": cur, "attribute": a,
                            "label": (labels.get(cur, cur) if path else "") +
                                     (attrs[a].get("label") or a),
                            "aliases": sorted({a, attrs[a].get("label") or a,
                                               labels.get(cur, cur)})})
        if len(path) >= MAX_DIM_HOPS:
            continue
        for rn, r in rels.items():
            if r["source"] == cur and r["cardinality"] == "many_to_one":
                if r["target"] == cur and path:  # 自引用只展开一层
                    continue
                key = (r["target"], rn)
                if r["target"] in visited and r["target"] != cur:
                    continue
                visited.add(r["target"])
                q.append((r["target"], path + [rn]))
    return out


def time_columns(schema: dict, cls: str) -> list[str]:
    return [a for a, m in schema["attributes"].items()
            if m["owner"] == cls and (m.get("role") == "temporal" or m.get("range") == "date")]


# ------------------------------------------------------------------ 生成
def generate(schema: dict, max_composite: int = 30) -> dict:
    labels = {k: v.get("label") or k for k, v in schema["entity_types"].items()}
    alab = {k: v.get("label") or k for k, v in schema["attributes"].items()}
    rlab = {k: v.get("label") or k for k, v in schema["relation_types"].items()}
    metrics: dict[str, dict] = {}

    def add(mid, **m):
        if not _VALID_ID.match(mid):
            mid = _snake(mid)
        if mid not in metrics:
            metrics[mid] = {"id": mid, **m}

    dims = {c: dimensions(schema, c) for c in schema["entity_types"]}

    # 1) 原子指标
    for cls in schema["entity_types"]:
        c = _snake(cls)
        add(f"{c}_count", kind="atomic", label=f"{labels[cls]}数", cls=cls, agg="count",
            measure=None, unit="个", reason="每个类的实例计数")
        for a, m in schema["attributes"].items():
            if m["owner"] != cls or m.get("role") != "measure":
                continue
            for agg in AGG_BY_ROLE["measure"]:
                add(f"{_snake(a)}_{agg}", kind="atomic", label=f"{ {'sum': '', 'avg': '平均', 'max': '最高'}[agg]}{alab[a]}{ {'sum': '合计', 'avg': '', 'max': ''}[agg]}",
                    cls=cls, agg=agg, measure=a, unit="", reason=f"度量列 {a}")
    for rn, r in schema["relation_types"].items():
        if r["cardinality"] == "many_to_many":
            add(f"{_snake(rn)}_links", kind="atomic", label=f"{rlab[rn]}数", cls=r["target"],
                agg="count_links", relation=rn, measure=None, unit="个",
                reason="多对多关系的关联条数（按目标类聚合）")
        if r["cardinality"] == "many_to_one" and r["source"] == r["target"]:
            add(f"{_snake(rn)}_sources", kind="atomic",
                label=("有上级的人员数" if rn == "reports_to" else f"有{rlab[rn]}的{labels[r['source']]}数"),
                cls=r["source"], agg="count_nonnull", relation=rn, measure=None, unit="个",
                reason=f"自引用关系 {rn} 的发起方数量")
            add(f"{_snake(rn)}_distinct_targets", kind="atomic",
                label=("上级人数" if rn == "reports_to" else f"被{rlab[rn]}的{labels[r['target']]}数"), cls=r["source"],
                agg="count_distinct", relation=rn, measure=None, unit="个",
                reason=f"自引用关系 {rn}：如上级/父节点数量")

    # 2) 派生：时间粒度 + 环比/同比
    for mid, m in list(metrics.items()):
        if m["kind"] != "atomic" or m["agg"] not in ("count", "sum", "avg"):
            continue
        for t in time_columns(schema, m["cls"])[:1]:
            for g in ("month", "year"):
                did = f"{mid}_by_{g}"
                add(did, kind="derived", label=f"{m['label']}（按{ {'month': '月', 'year': '年'}[g]}，{t}）",
                    base=mid, time=t, grain=g, cls=m["cls"], unit=m["unit"],
                    reason=f"{m['cls']} 有时间属性 {t}")
            add(f"{mid}_mom", kind="derived", label=f"{m['label']}月环比", base=f"{mid}_by_month",
                transform="pct_change", periods=1, cls=m["cls"], unit="%", reason="月度环比")
            add(f"{mid}_yoy", kind="derived", label=f"{m['label']}同比", base=f"{mid}_by_month",
                transform="pct_change", periods=12, cls=m["cls"], unit="%", reason="同比（12 期）")

    # 3) 复合
    comp = []
    atoms = [m for m in metrics.values() if m["kind"] == "atomic"]
    counts = {m["cls"]: m for m in atoms if m["agg"] == "count"}
    for m in atoms:
        # 3a 人均 / 每X：sum(度量) / count(B)，要求度量所在类沿 many_to_one 可达 B
        #     （Project →owned_by→ Person ⇒ 人均预算；同类相除即 avg，跳过）
        if m["agg"] == "sum":
            for cls, cm in counts.items():
                if cls == m["cls"] or not _reaches(schema, m["cls"], cls):
                    continue
                common = frame_dims(schema, m["cls"], cls)
                comp.append((len(common) + 3, f"{m['measure']}_per_{_snake(cls)}",
                             dict(kind="composite",
                                  label=(f"人均{alab[m['measure']]}" if cls == "Person"
                                         else f"每{labels[cls]}{alab[m['measure']]}"),
                                  expr=f"{m['id']} / {cm['id']}", unit="",
                                  dims_allowed=common, frame=cls, num_cls=m["cls"],
                                  reason=f"{m['cls']} 经关系可达 {cls}，共享维度 {len(common)} 个")))
        # 3b 占比
            comp.append((5, f"{m['measure']}_share", dict(
                kind="composite", label=f"{alab[m['measure']]}占比", expr=f"{m['id']} / total({m['id']})",
                unit="%", dims_allowed=[d["id"] for d in dims[m["cls"]] if d["path"] or d["attribute"] not in _names(schema)], frame=m["cls"],
                reason="维度内占总量比例")))
        # 3c 管理幅度：count / count_distinct(自引用目标)
        if m["agg"] == "count_distinct":
            base = metrics.get(f"{m['relation']}_sources") or counts[m["cls"]]
            comp.append((9, f"span_of_{m['relation']}", dict(
                kind="composite", label=("平均管理幅度" if m["relation"] == "reports_to" else f"平均{rlab[m['relation']]}幅度"),
                expr=f"{base['id']} / {m['id']}", unit="", frame=m["cls"],
                dims_allowed=[d["id"] for d in dims[m["cls"]] if d["path"] or d["attribute"] not in _names(schema)],
                reason=f"自引用关系 {m['relation']}：有该关系的成员数 / 被指向者数")))
        # 3d 关联密度：links / 目标类计数（如人均参与项目数、项目平均成员数）
        if m["agg"] == "count_links":
            base = counts.get(m["cls"])
            if base:
                comp.append((8, f"{m['relation']}_per_{_snake(m['cls'])}", dict(
                    kind="composite", label=f"{labels[m['cls']]}平均{labels[schema['relation_types'][m['relation']]['source']]}数",
                    expr=f"{m['id']} / {base['id']}", unit="", frame=m["cls"],
                    dims_allowed=[d["id"] for d in dims[m["cls"]] if d["path"] or d["attribute"] not in _names(schema)],
                    reason="多对多关联密度")))
    # 3e 类间比率：count(A) / count(B)，仅当 A 沿 many_to_one 可达 B（如 人员/组织单元 = 平均部门规模）
    for a in counts:
        for b in counts:
            if a == b or not _reaches(schema, a, b):
                continue
            common = frame_dims(schema, a, b)
            comp.append((len(common) + 2, f"{_snake(a)}_per_{_snake(b)}", dict(
                kind="composite", label=f"平均每{labels[b]}{labels[a]}数",
                expr=f"{counts[a]['id']} / {counts[b]['id']}", unit="",
                dims_allowed=common, frame=b, num_cls=a, reason=f"{a} 经关系隶属 {b}")))
    for _, mid, m in sorted(comp, key=lambda x: -x[0])[:max_composite]:
        add(mid, **m)

    names = _names(schema)
    for m in metrics.values():
        m["deps"] = deps_of(m)
        # 默认维度不含对象自身名称（按「每个员工」分组的人数恒为 1，无分析意义）
        m.setdefault("dims_allowed", [d["id"] for d in dims.get(m.get("cls"), [])
                                      if d["path"] or d["attribute"] not in names])
    return {"metrics": metrics, "dimensions": dims,
            "issues": validate(metrics),
            "stats": {k: sum(1 for m in metrics.values() if m["kind"] == k)
                      for k in ("atomic", "derived", "composite")}}


def _names(schema: dict) -> set[str]:
    """实体自身的名称列（每个实例唯一的那一列），不是所有 role=name 的列：
    宽表里「岗位名称 / 部门名称 / 学历名称」也是 name 角色，但它们是分类维度。"""
    own = {v.get("name_column") for v in schema["entity_types"].values() if v.get("name_column")}
    return {a for a, m in schema["attributes"].items() if m.get("column") in own or a in own}


def _path(schema: dict, a: str, b: str, hops: int = MAX_DIM_HOPS) -> list[str] | None:
    """a → b 的最短 many_to_one 关系路径（不含自引用）。"""
    q = deque([(a, [])])
    seen = {a}
    while q:
        cur, path = q.popleft()
        if cur == b and path:
            return path
        if len(path) >= hops:
            continue
        for rn, r in schema["relation_types"].items():
            if r["cardinality"] == "many_to_one" and r["source"] == cur \
                    and r["target"] != cur and r["target"] not in seen:
                seen.add(r["target"])
                q.append((r["target"], path + [rn]))
    return None


def frame_dims(schema: dict, num_cls: str, den_cls: str) -> list[str]:
    """以分母类为参照系的维度：分母按 d 分组，分子按 path(A→B)+d 分组；两边都存在才保留。"""
    names = _names(schema)
    own = lambda d: not d["path"] and d["attribute"] in names   # 参照系自身名称：每组恒为单个实例
    if num_cls == den_cls:
        return [d["id"] for d in dimensions(schema, den_cls) if not own(d)]
    pre = _path(schema, num_cls, den_cls)
    if pre is None:
        return []
    num = {(tuple(d["path"]), d["attribute"]) for d in dimensions(schema, num_cls)}
    return [d["id"] for d in dimensions(schema, den_cls)
            if not own(d) and (tuple(pre + d["path"]), d["attribute"]) in num]


def _reaches(schema: dict, a: str, b: str, hops: int = MAX_DIM_HOPS) -> bool:
    """a 沿 many_to_one 关系在 hops 跳内能否到达 b（不含自引用）。"""
    frontier, seen = {a}, {a}
    for _ in range(hops):
        nxt = {r["target"] for r in schema["relation_types"].values()
               if r["cardinality"] == "many_to_one" and r["source"] in frontier
               and r["target"] != r["source"]} - seen
        if b in nxt:
            return True
        seen |= nxt
        frontier = nxt
    return False


def _common_dims(dims: dict, a: str, b: str) -> list[str]:
    """两个类的共同维度：落在同一 (class, attribute) 上即可对齐。"""
    ka = {(d["class"], d["attribute"]): d["id"] for d in dims[a]}
    kb = {(d["class"], d["attribute"]) for d in dims[b]}
    return [ka[k] for k in ka if k in kb]


def deps_of(m: dict) -> list[str]:
    if m["kind"] == "derived":
        return [m["base"]]
    if m["kind"] == "composite":
        return sorted({t for t in _IDENT.findall(m["expr"]) if t != "total"})
    return []


# ------------------------------------------------------------------ 校验
def validate(metrics: dict) -> list[dict]:
    issues = []
    for mid, m in metrics.items():
        for d in m.get("deps") or deps_of(m):
            if d not in metrics:
                issues.append({"level": "error", "metric": mid, "msg": f"依赖 {d} 不存在"})
        if m["kind"] == "composite" and not m.get("dims_allowed"):
            issues.append({"level": "info", "metric": mid, "msg": "无可下钻维度，仅提供总量"})
    # 环检测
    state = {}

    def dfs(x, stack):
        state[x] = 1
        for d in metrics.get(x, {}).get("deps") or []:
            if state.get(d) == 1:
                issues.append({"level": "error", "metric": x, "msg": "循环依赖：" + "→".join(stack + [d])})
            elif d in metrics and not state.get(d):
                dfs(d, stack + [d])
        state[x] = 2
    for mid in metrics:
        if not state.get(mid):
            dfs(mid, [mid])
    return issues


# ------------------------------------------------------------------ 计算（基于物化结果）
def frames(materialized: dict, schema: dict):
    """物化的 entities/facts → 每个类一张宽表 + 多对多关联表。"""
    import pandas as pd
    rels = schema["relation_types"]
    rows: dict[str, list] = {}
    idx = {}
    for e in materialized["entities"]:
        r = {"_id": e["external_id"], "_name": e["canonical_name"], **e["properties"]}
        rows.setdefault(e["entity_type"], []).append(r)
        idx[e["external_id"]] = r
    links: dict[str, list] = {}
    for f in materialized["facts"]:
        ov = f["object_value"]
        if "external_id" not in ov:
            continue
        spec = rels.get(f["predicate"]) or {}
        if spec.get("cardinality") == "many_to_many":
            links.setdefault(f["predicate"], []).append(
                {"source": f["subject_external_id"], "target": ov["external_id"]})
        elif f["subject_external_id"] in idx:
            idx[f["subject_external_id"]][f["predicate"]] = ov["external_id"]
    return ({c: pd.DataFrame(v) for c, v in rows.items()},
            {k: pd.DataFrame(v) for k, v in links.items()})


def _attach_dim(df, dim: dict, F: dict, schema: dict):
    """沿 dim.path 逐跳 join，得到维度列 'dim'。"""
    cur = df.copy()
    key = "_id"
    cls = None
    for rn in dim["path"]:
        tgt = schema["relation_types"][rn]["target"]
        right = F[tgt][["_id", *[c for c in F[tgt].columns if c != "_id"]]].add_prefix(f"{rn}.")
        col = rn if key == "_id" else f"{key}.{rn}"
        cur = cur.merge(right, how="left", left_on=col, right_on=f"{rn}.{'_id'}")
        key = rn
        cls = tgt
    col = dim["attribute"] if not dim["path"] else f"{dim['path'][-1]}.{dim['attribute']}"
    if dim["path"] and dim["attribute"] not in F[cls].columns:
        col = f"{dim['path'][-1]}._name"
    cur["dim"] = cur[col].fillna("(空)") if col in cur else "(空)"
    return cur


def _period(series, grain: str):
    import pandas as pd
    s = pd.to_datetime(series, errors="coerce")
    return {"month": s.dt.to_period("M"), "quarter": s.dt.to_period("Q"),
            "year": s.dt.to_period("Y")}[grain].astype(str)


def evaluate(mid: str, metrics: dict, F: dict, L: dict, schema: dict,
             dim: str | None = None):
    """返回 pandas.Series（按维度/周期）或标量。"""
    import pandas as pd
    m = metrics[mid]
    dims = {d["id"]: d for d in dimensions(schema, m.get("cls"))} if m.get("cls") else {}

    if m["kind"] == "atomic":
        if m["agg"] == "count_links":
            lk = L.get(m["relation"])
            if lk is None or lk.empty:
                return 0
            df = F[m["cls"]].merge(lk.groupby("target").size().rename("_n"),
                                   how="left", left_on="_id", right_index=True).fillna({"_n": 0})
            val, agg = "_n", "sum"
        else:
            df, val, agg = F[m["cls"]], m.get("measure"), m["agg"]
        if dim:
            df = _attach_dim(df, dims[dim], F, schema)
        if agg == "count":
            return df.groupby("dim").size() if dim else len(df)
        if agg == "count_distinct":
            col = m["relation"]
            return df.groupby("dim")[col].nunique() if dim else df[col].nunique()
        if agg == "count_nonnull":
            col = m["relation"]
            if col not in df:
                return 0
            return df.groupby("dim")[col].count() if dim else int(df[col].count())
        fn = {"sum": "sum", "avg": "mean", "max": "max"}[agg]
        s = pd.to_numeric(df[val], errors="coerce")
        if dim:
            return s.groupby(df["dim"]).agg(fn).round(2)
        return round(float(s.agg(fn)), 2)

    if m["kind"] == "derived" and m.get("grain"):
        base = metrics[m["base"]]
        df = F[base["cls"]].copy()
        df["_p"] = _period(df[m["time"]], m["grain"])
        if base["agg"] == "count":
            return df.groupby("_p").size().sort_index()
        fn = {"sum": "sum", "avg": "mean", "max": "max"}[base["agg"]]
        return pd.to_numeric(df[base["measure"]], errors="coerce").groupby(df["_p"]).agg(
            fn).round(2).sort_index()
    if m["kind"] == "derived" and m.get("transform") == "pct_change":
        s = evaluate(m["base"], metrics, F, L, schema)
        return (s.pct_change(periods=m["periods"]) * 100).round(2)

    if m["kind"] == "composite":
        return _eval_expr(m["expr"], lambda x: evaluate(
            x, metrics, F, L, schema, _align_dim(dim, m, metrics[x], schema, metrics)))
    raise ValueError(f"无法计算 {mid}")


def _align_dim(dim, comp, dep, schema, metrics):
    """复合指标的维度 id 定义在参照系类 frame 上；换算到依赖指标所在类。"""
    if not dim:
        return None
    dep_cls = _root(dep, metrics)["cls"]
    frame = comp.get("frame") or comp.get("cls") or dep_cls
    if dep_cls == frame:
        return dim
    d = next(x for x in dimensions(schema, frame) if x["id"] == dim)
    pre = _path(schema, dep_cls, frame) or []
    want = (tuple(pre + d["path"]), d["attribute"])
    for x in dimensions(schema, dep_cls):
        if (tuple(x["path"]), x["attribute"]) == want:
            return x["id"]
    raise ValueError(f"维度 {dim} 不适用于 {dep['id']}")


def _root(m, metrics):
    while m["kind"] != "atomic":
        m = metrics[m["deps"][0]]
    return m


def _eval_expr(expr: str, get):
    """只支持 A / B 与 A / total(A)；不 eval 任意表达式。"""
    m = re.fullmatch(r"\s*(\w+)\s*/\s*(total\()?(\w+)\)?\s*", expr)
    if not m:
        raise ValueError(f"不支持的表达式：{expr}")
    a, is_total, b = m.group(1), bool(m.group(2)), m.group(3)
    num = get(a)
    den = get(b)
    if is_total:
        tot = den.sum() if hasattr(den, "sum") else den
        if not tot:
            return num * 0
        out = num / tot * 100
        return out.round(2) if hasattr(out, "round") else round(out, 2)
    if not hasattr(num, "replace") and not hasattr(den, "replace") and not den:
        return None                                   # 分母为 0：无定义，不返回 inf
    out = num / den
    if hasattr(out, "replace"):
        import numpy as np
        return out.replace([np.inf, -np.inf], np.nan).round(3)
    return round(out, 3) if den else None


# ------------------------------------------------------------------ SQL 编译（对源表，供问答复用）
def to_sql(mid: str, metrics: dict, schema: dict, contract: dict, dim: str | None = None) -> str:
    """原子/时间派生 → SQL（标识符来自契约，非用户输入；仍做白名单校验）。"""
    m = metrics[mid]
    if m["kind"] == "derived" and m.get("grain"):
        base = metrics[m["base"]]
        tbl, cols = _table_of(base["cls"], contract)
        t = _q(cols[m["time"]])
        unit = {"month": "month", "quarter": "quarter", "year": "year"}[m["grain"]]
        sel = "COUNT(*)" if base["agg"] == "count" else f"{base['agg'].upper()}({_q(cols[base['measure']])})"
        return (f"SELECT date_trunc('{unit}', CAST({t} AS date)) AS period, {sel} AS value\n"
                f"FROM {_q(tbl)} GROUP BY 1 ORDER BY 1")
    if m["kind"] != "atomic" or m["agg"] not in ("count", "sum", "avg", "max"):
        raise NotImplementedError("复合/关联类指标由执行器按依赖组合计算")
    tbl, cols = _table_of(m["cls"], contract)
    sel = "COUNT(*)" if m["agg"] == "count" else f"{m['agg'].upper()}(t0.{_q(cols[m['measure']])})"
    if not dim:
        return f"SELECT {sel} AS value FROM {_q(tbl)} t0"
    d = {x["id"]: x for x in dimensions(schema, m["cls"])}[dim]
    joins, alias, cls = [], "t0", m["cls"]
    for i, rn in enumerate(d["path"], 1):
        r = schema["relation_types"][rn]
        _, ccols = _table_of(cls, contract)
        ttbl, tcols = _table_of(r["target"], contract)
        fk = next(x["column"] for x in _mapping(cls, contract)["relations"] if x["type"] == rn)
        pk = _mapping(r["target"], contract)["pk"]
        joins.append(f"LEFT JOIN {_q(ttbl)} t{i} ON t{i}.{_q(pk)} = {alias}.{_q(fk)}")
        alias, cls = f"t{i}", r["target"]
    _, dcols = _table_of(cls, contract)
    dcol = dcols.get(d["attribute"], d["attribute"])
    return (f"SELECT {alias}.{_q(dcol)} AS dim, {sel} AS value\nFROM {_q(tbl)} t0\n"
            + "\n".join(joins) + f"\nGROUP BY 1 ORDER BY 2 DESC")


def _mapping(cls: str, contract: dict) -> dict:
    m = next(x for x in contract["mappings"] if x["class"] == cls)
    pk = _IDENT.findall(m["id"].split(":")[-1])[0]
    return {**m, "pk": pk}


def _table_of(cls: str, contract: dict) -> tuple[str, dict]:
    m = _mapping(cls, contract)
    cols = dict(m["attributes"])
    cols[m["pk"]] = m["pk"]
    return m["table"], cols


def _q(ident: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ident or ""):
        raise ValueError(f"非法标识符：{ident!r}")
    return '"' + ident + '"'


# ------------------------------------------------------------------ 血缘（可视化）
def lineage(result: dict, schema: dict) -> dict:
    """本体元素 → 原子 → 派生 → 复合 的 DAG，ECharts graph 结构。"""
    nodes, links = [], []
    cats = ["类", "度量", "原子指标", "派生指标", "复合指标"]
    seen = set()

    def node(nid, name, cat, **kw):
        if nid not in seen:
            seen.add(nid)
            nodes.append({"id": nid, "name": name, "category": cats.index(cat), **kw})

    for mid, m in result["metrics"].items():
        cat = {"atomic": "原子指标", "derived": "派生指标", "composite": "复合指标"}[m["kind"]]
        node(mid, m["label"], cat, expr=m.get("expr") or m.get("agg") or m.get("transform"),
             reason=m.get("reason"))
        if m["kind"] == "atomic":
            cl = m["cls"]
            node(f"class:{cl}", schema["entity_types"][cl].get("label") or cl, "类")
            src = f"class:{cl}"
            if m.get("measure"):
                node(f"attr:{m['measure']}", m["measure"], "度量")
                links.append({"source": f"class:{cl}", "target": f"attr:{m['measure']}"})
                src = f"attr:{m['measure']}"
            links.append({"source": src, "target": mid})
        for d in m["deps"]:
            links.append({"source": d, "target": mid})
    return {"nodes": nodes, "links": links, "categories": [{"name": c} for c in cats]}
