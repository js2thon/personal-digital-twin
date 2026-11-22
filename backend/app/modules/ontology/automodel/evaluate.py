"""本体评测：由本体 + 实例数据自动生成评测问题集（带标准答案），并用问答引擎逐题测试打分。

标准答案独立于问答引擎计算（直接对物化实例做 pandas 统计 / 查找），避免「自己出题自己判」。
题型覆盖（每类按本体自动取样）：
- attr_lookup   实体属性查询：「{姓名}的{岗位}是什么」
- relation      关系查询：「{员工}的{上级/部门}是谁」（沿 many_to_one 关系）
- count_all     类计数：「一共有多少{人员}」
- filter_count  按属性取值过滤计数：「{数据合规组}有多少人」（同值多列取并集，与层级部门语义一致）
- group_dist    分组分布：「{性别}分布 / 各{学历}人数」
- filter_group  过滤 + 分组：「{数据管理部}男女占比」
- avg / sum / max   度量聚合：「平均{年龄}」
- filter_avg    过滤 + 聚合：「{数据合规组}的平均{年龄}」
- neighbor_count 关系聚合：「{研发部}有多少人」（多类本体）
- negative      本体外问题：应拒答
判分：数值题按相对误差 ≤ 0.5% ；分组题要求每组计数一致；查询题要求答案包含标准值；拒答题要求 ok=False。
"""
from __future__ import annotations

import math
import random
import re
import time

from . import graphqa as gq

TOL = 0.005


def _norm(v) -> str:
    return gq._norm(v)


def _isnum(v) -> bool:
    try:
        return v is not None and not (isinstance(v, float) and math.isnan(v)) and float(v) == float(v)
    except (TypeError, ValueError):
        return False


def _words(schema, a):
    """出题用词：优先属性中文标签（「性别」「最高学历」），其次词典说法。"""
    lab = schema["attributes"][a].get("label") or ""
    if lab and re.search(r"[\u4e00-\u9fff]", lab) and "（" not in lab and len(lab) <= 8:
        return lab
    w = gq.attr_words(schema, a)
    return w[0] if w else (lab or a)


def _good_attrs(schema, df, cls, roles=("enum", "name")):
    out = []
    names = {v.get("name_column") for v in schema["entity_types"].values()}
    for a, m in schema["attributes"].items():
        if m["owner"] != cls or a not in df or m.get("role") not in roles or a in names:
            continue
        if not gq.attr_words(schema, a) or gq._attr_rank(schema, a) < 0:
            continue
        nun = df[a].nunique()
        if 1 < nun <= 30:
            out.append(a)
    out.sort(key=lambda a: -gq._attr_rank(schema, a))
    seen, res = set(), []
    for a in out:                                # 同一说法（岗位/部门…）只留最优列
        k = _words(schema, a)
        if k not in seen:
            seen.add(k)
            res.append(a)
    return res


def _measures(schema, df, cls):
    return [a for a, m in schema["attributes"].items()
            if m["owner"] == cls and m.get("role") == "measure" and a in df
            and gq.attr_words(schema, a) and pd_num(df[a]).notna().sum() > 1]


def pd_num(s):
    import pandas as pd
    return pd.to_numeric(s, errors="coerce")


def _cls_word(schema, cls):
    ws = [w for w in gq._class_words(schema, cls) if re.search(r"[\u4e00-\u9fff]", str(w))]
    return "人员" if cls == "Person" or any(w in ("人员", "员工") for w in ws) else (ws[0] if ws else cls)


def _people(schema, cls):
    ws = gq._class_words(schema, cls)
    return cls == "Person" or any(w in ("人员", "员工", "人") for w in ws) or "人员" in (schema["entity_types"][cls].get("label") or "")


def _filter_union(schema, df, cls, value):
    """标准答案的过滤口径：任一 enum/name 列等于该值（忽略括注）即命中。"""
    import pandas as pd
    nv = _norm(value)
    m = pd.Series(False, index=df.index)
    for a, meta in schema["attributes"].items():
        if meta["owner"] == cls and a in df and meta.get("role") in ("enum", "name", "text"):
            m |= df[a].astype(str).map(_norm) == nv
    return df[m]


# ------------------------------------------------------------------ 生成
def generate(schema: dict, F: dict, n_per_type: int = 3, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    qs: list[dict] = []

    def add(kind, q, gold, **kw):
        qs.append({"id": f"Q{len(qs) + 1:03d}", "kind": kind, "question": q, "gold": gold, **kw})

    classes = [c for c in schema["entity_types"] if c in F and len(F[c])]
    for cls in classes:
        df = F[cls]
        cw = _cls_word(schema, cls)
        people = _people(schema, cls)
        named = df[df["_name"].astype(str).map(_norm).str.len() >= 2]
        # 名称唯一的实例才出查询题
        uniq = named[~named["_name"].astype(str).map(_norm).duplicated(keep=False)]
        pn0 = set(df["_name"].astype(str).map(_norm))
        attrs = [a for a in _good_attrs(schema, df, cls)
                 if not (set(df[a].dropna().astype(str).map(_norm)) & pn0)]
        meas = _measures(schema, df, cls)

        # count_all
        add("count_all", f"一共有多少{'人' if people else cw}？", {"type": "number", "value": len(df)}, cls=cls)

        # attr_lookup
        for a in attrs[:n_per_type]:
            rows = uniq[uniq[a].notna()]
            if rows.empty:
                continue
            r = rows.iloc[rnd.randrange(len(rows))]
            add("attr_lookup", f"{_norm(r['_name'])}的{_words(schema, a)}是什么？",
                {"type": "contains", "value": _norm(r[a])}, cls=cls, attribute=a)

        # relation (many_to_one from this class)
        rels = [(rn, v) for rn, v in schema["relation_types"].items()
                if v["source"] == cls and v.get("cardinality") == "many_to_one" and rn in df]
        for rn, v in rels[:n_per_type]:
            rows = uniq[uniq[rn].notna()]
            if rows.empty:
                continue
            r = rows.iloc[rnd.randrange(len(rows))]
            tgt = F.get(v["target"])
            hit = tgt[tgt["_id"] == r[rn]] if tgt is not None else None
            if hit is None or hit.empty:
                continue
            word = (gq._rel_words(schema, v.get("label") or rn) or [v.get("label") or rn])[0]
            add("relation", f"{_norm(r['_name'])}的{word}是谁？" if v["target"] == cls or _people(schema, v["target"])
                else f"{_norm(r['_name'])}的{word}是什么？",
                {"type": "contains", "value": _norm(hit.iloc[0]["_name"])}, cls=cls, relation=rn)

        # group_dist
        for a in attrs[:n_per_type]:
            vc = df[a].fillna("空值").astype(str).map(_norm).replace("", "空值").value_counts()
            add("group_dist", f"各{_words(schema, a)}的{'人数' if people else '数量'}分布？",
                {"type": "groups", "value": {k: int(v) for k, v in vc.items()}}, cls=cls, attribute=a)

        # filter values: pick values covering ≥2 rows, prefer hierarchical (many columns)
        vals = []
        person_names = set(df["_name"].astype(str).map(_norm))
        for a in attrs + [x for x, m in schema["attributes"].items() if m["owner"] == cls and x in df
                          and m.get("role") == "name" and x not in attrs]:
            if set(df[a].dropna().astype(str).map(_norm)) & person_names:
                continue                             # 人名列（负责人/上级姓名）不作为过滤分组
            for v, c in df[a].dropna().astype(str).value_counts().items():
                nv = _norm(v)
                if 2 <= len(nv) <= 20 and 2 <= c < len(df) and not nv.isdigit() \
                        and "_en_" not in a and (re.search(r"[\u4e00-\u9fff]", nv)
                                                 or schema["attributes"][a].get("role") == "enum"):
                    vals.append((nv, a))
        rnd.shuffle(vals)
        # 有区分度的值优先：部门 / 岗位 / 职级 / 学历等业务分组列排在前面
        pri = re.compile(r"(dept|部门|post|岗位|grade|职级|educ|学历|job|职务|level|project|项目|region|区域)", re.I)
        vals.sort(key=lambda x: 0 if pri.search(x[1] + (schema["attributes"][x[1]].get("label") or "")) else 1)
        seen_v = set()
        fvals = []
        for nv, a in vals:
            if nv not in seen_v:
                seen_v.add(nv)
                fvals.append((nv, a))
        # 也加入覆盖全体的上级层级值（如「数据管理部」）
        for a, m in schema["attributes"].items():
            if not re.search(r"(dept|部门|org|组织)", a + (m.get("label") or ""), re.I):
                continue                             # 只把「全员同属的上级部门」作为层级过滤题
            if m["owner"] == cls and a in df and df[a].nunique() == 1 and m.get("role") in ("name", "enum") \
                    and not (set(df[a].dropna().astype(str).map(_norm)) & person_names):
                v = _norm(df[a].dropna().astype(str).iloc[0]) if df[a].notna().any() else ""
                if 2 <= len(v) <= 20 and v not in seen_v and re.search(r"[\u4e00-\u9fff]", v):
                    seen_v.add(v)
                    fvals.insert(0, (v, a))
        # 保留 1 个「全员同属」的上级层级值（考察层级并集口径），其余必须真正有区分度（子集 < 全体）
        size = {x[0]: len(_filter_union(schema, df, cls, x[0])) for x in fvals}
        top = [x for x in fvals if size[x[0]] == len(df) and re.search(r"(dept_cn_name|dept_name|部门)", x[1], re.I)
               and not re.search(r"(country|company|status|国家|公司|状态)", x[1], re.I)][:1]
        rest = [x for x in fvals if 2 <= size[x[0]] < len(df)]
        fvals = top + rest
        for nv, a in fvals[:n_per_type]:
            sub = _filter_union(schema, df, cls, nv)
            add("filter_count", f"{nv}有多少{'人' if people else '个' + cw}？", {"type": "number", "value": len(sub)},
                cls=cls, filter=nv)
            g = next((x for x in attrs if x != a and "性别" in _words(schema, x) + (schema["attributes"][x].get("label") or "")),
                     next((x for x in attrs if x != a), None))
            if g:
                vc = sub[g].fillna("空值").astype(str).map(_norm).replace("", "空值").value_counts()
                gw = "男女" if "性别" in (schema["attributes"][g].get("label") or "") + _words(schema, g) else _words(schema, g)
                add("filter_group", f"{nv}{gw}占比多少？" if gw == "男女" else f"{nv}的{gw}分布？",
                    {"type": "groups", "value": {k: int(v) for k, v in vc.items()}}, cls=cls, filter=nv, attribute=g)
            for mm in meas[:1]:
                s = pd_num(sub[mm]).dropna()
                if len(s):
                    add("filter_avg", f"{nv}的平均{_words(schema, mm)}是多少？", {"type": "number", "value": float(s.mean())},
                        cls=cls, filter=nv, measure=mm)

        # aggregates
        for mm in meas[:n_per_type]:
            s = pd_num(df[mm]).dropna()
            w = _words(schema, mm)
            add("avg", f"平均{w}是多少？", {"type": "number", "value": float(s.mean())}, cls=cls, measure=mm)
            add("max", f"最高{w}是多少？", {"type": "number", "value": float(s.max())}, cls=cls, measure=mm)
            if not re.search(r"(年龄|age)", w + mm, re.I):
                add("sum", f"{w}合计是多少？", {"type": "number", "value": float(s.sum())}, cls=cls, measure=mm)

    # neighbor_count：多类本体中，「目标实体有多少个来源类实例」
    for rn, v in schema["relation_types"].items():
        if v.get("cardinality") != "many_to_one" or v["source"] == v["target"]:
            continue
        src, tgt = F.get(v["source"]), F.get(v["target"])
        if src is None or tgt is None or rn not in src or tgt.empty:
            continue
        vc = src[rn].dropna().value_counts()
        cand = [t for t in tgt.to_dict(orient="records") if len(_norm(t["_name"])) >= 2 and t["_id"] in vc.index]
        for t in cand[:2]:
            sw = _cls_word(schema, v["source"])
            add("neighbor_count", f"{_norm(t['_name'])}有多少{'人' if _people(schema, v['source']) else '个' + sw}？",
                {"type": "number", "value": int(vc[t["_id"]])}, cls=v["source"], relation=rn)
        break

    # ---------------- 扩展题型：排名 / 时间范围 / 多条件 / 多跳关系
    for cls in classes:
        df = F[cls]
        people = _people(schema, cls)
        pn0 = set(df["_name"].astype(str).map(_norm))
        attrs = [a for a in _good_attrs(schema, df, cls) if not (set(df[a].dropna().astype(str).map(_norm)) & pn0)]
        meas = _measures(schema, df, cls)
        # top_n：人数最多的 {属性}
        for a in attrs[:2]:
            vc = df[a].dropna().astype(str).map(_norm)
            vc = vc[vc != ""].value_counts()
            if len(vc) >= 2 and vc.iloc[0] > vc.iloc[1]:
                add("top_n", f"哪个{_words(schema, a)}的{'人数' if people else '数量'}最多？",
                    {"type": "contains", "value": vc.index[0]}, cls=cls, attribute=a)
        # top_n：平均度量最高的 {属性}
        for mm in meas[:1]:
            for a in attrs[:1]:
                g = pd_num(df[mm]).groupby(df[a].astype(str).map(_norm)).mean().dropna()
                g = g[~g.index.isin(["", "nan", "None"])]
                if len(g) >= 2 and g.sort_values().iloc[-1] > g.sort_values().iloc[-2]:
                    add("top_n", f"哪个{_words(schema, a)}的平均{_words(schema, mm)}最高？",
                        {"type": "contains", "value": g.idxmax()}, cls=cls, attribute=a, measure=mm)
        # time_range：{年}后{时间属性}的有多少
        tcols = [a for a, m in schema["attributes"].items() if m["owner"] == cls and a in df
                 and (m.get("role") == "temporal" or m.get("range") == "date") and gq.attr_words(schema, a)]
        for a in tcols[:1]:
            import pandas as pd
            ts = pd.to_datetime(df[a], errors="coerce").dropna()
            if len(ts) < 4:
                continue
            y = int(ts.dt.year.median())
            n = int((pd.to_datetime(df[a], errors="coerce") >= pd.Timestamp(f"{y}-01-01")).sum())
            w = _words(schema, a).replace("日期", "").replace("时间", "") or _words(schema, a)
            add("time_range", f"{y}年后{w}的有多少{'人' if people else '个'}？", {"type": "number", "value": n},
                cls=cls, attribute=a)
        # multi_filter：{取值A}的{取值B}有多少（两个不同属性同时过滤）
        pairs = []
        for a in attrs[:4]:
            for b in attrs[:4]:
                if a >= b:
                    continue
                for va in df[a].dropna().astype(str).map(_norm).value_counts().index[:2]:
                    for vb in df[b].dropna().astype(str).map(_norm).value_counts().index[:2]:
                        if len(va) >= 2 and len(vb) >= 1 and va != vb:
                            sub = df[(df[a].astype(str).map(_norm) == va) & (df[b].astype(str).map(_norm) == vb)]
                            if 1 <= len(sub) < len(df):
                                pairs.append((va, vb, len(sub)))
        rnd.shuffle(pairs)
        for va, vb, n in pairs[:2]:
            vbw = {"男": "男性", "女": "女性"}.get(vb, vb)
            add("multi_filter", f"{va}的{vbw}有多少{'人' if people else '个'}？", {"type": "number", "value": n},
                cls=cls, filters=[va, vb])
    # multi_hop：{实体}的{关系1}的{关系2}（如「员工5的上级在哪个部门」）
    for r1, v1 in schema["relation_types"].items():
        if v1.get("cardinality") != "many_to_one" or v1["source"] != v1["target"]:
            continue
        for r2, v2 in schema["relation_types"].items():
            if r2 == r1 or v2.get("cardinality") != "many_to_one" or v2["source"] != v1["target"]:
                continue
            src = F[v1["source"]]
            tgt2 = F.get(v2["target"])
            if tgt2 is None or r1 not in src or r2 not in src:
                continue
            idx = src.set_index("_id")
            rows = src[src[r1].notna()]
            for _, r in rows.sample(min(2, len(rows)), random_state=seed).iterrows():
                mid = r[r1]
                if mid not in idx.index or not isinstance(idx.loc[mid, r2], str):
                    continue
                hit = tgt2[tgt2["_id"] == idx.loc[mid, r2]]
                if hit.empty:
                    continue
                w1 = (gq._rel_words(schema, v1.get("label") or r1) or ["上级"])[0]
                add("multi_hop", f"{_norm(r['_name'])}的{w1}在哪个部门？" if "部门" in "".join(gq._rel_words(schema, v2.get("label") or r2))
                    else f"{_norm(r['_name'])}的{w1}的{(gq._rel_words(schema, v2.get('label') or r2) or [r2])[0]}是什么？",
                    {"type": "contains", "value": _norm(hit.iloc[0]["_name"])}, cls=v1["source"], relation=[r1, r2])
            break
        break

    for q in ["今天天气怎么样？", "帮我订一张去北京的机票", "公司的股价是多少？"]:
        add("negative", q, {"type": "refuse"})
    # 去重（同一问题只保留一次）
    seen, out = set(), []
    for q in qs:
        if q["question"] not in seen:
            seen.add(q["question"])
            out.append(q)
    for i, q in enumerate(out, 1):
        q["id"] = f"Q{i:03d}"
    return out


# ------------------------------------------------------------------ 判分
def _nums_in(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*", text or "")]


def _close(a, b) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(TOL * abs(b), 0.01 + (0.5 if abs(b) >= 100 else 0))


def extract_number(r: dict):
    res = r.get("result") or {}
    if isinstance(res, dict):
        if "value" in res and _isnum(res["value"]):
            return float(res["value"])
        if "count" in res and _isnum(res["count"]):
            return float(res["count"])
        if res.get("items") and _isnum(res["items"][0].get("count")):
            return float(res["items"][0]["count"])
        if res.get("op") == "lookup" and r.get("count_question"):
            pass
    return None


def extract_groups(r: dict) -> dict | None:
    res = r.get("result") or {}
    if res.get("op") == "group":
        return {g["value"]: g["count"] for g in res.get("groups") or []}
    if res.get("kind") == "series":
        return dict(zip(res.get("index") or [], res.get("values") or []))
    return None


def score(q: dict, r: dict) -> dict:
    g = q["gold"]
    ans = r.get("answer") or r.get("error") or ""
    if g["type"] == "refuse":
        ok = not r.get("ok")
        return {"pass": ok, "reason": "正确拒答" if ok else "本体外问题未拒答"}
    if not r.get("ok"):
        return {"pass": False, "reason": f"未作答：{ans[:80]}"}
    if g["type"] == "number":
        v = extract_number(r)
        if v is None:
            nums = _nums_in(ans)
            v = next((x for x in nums if _close(x, g["value"])), nums[0] if nums else None)
        ok = _close(v, g["value"])
        return {"pass": ok, "got": v, "reason": "数值一致" if ok else f"期望 {round(g['value'], 2)}，得到 {v}"}
    if g["type"] == "contains":
        ok = g["value"] and _norm(g["value"]) in _norm(ans)
        return {"pass": bool(ok), "reason": "答案包含标准值" if ok else f"未包含「{g['value']}」"}
    if g["type"] == "groups":
        got = extract_groups(r) or {}
        got = {_norm(k): v for k, v in got.items()}
        want = g["value"]
        diff = {k: (want.get(k), got.get(k)) for k in set(want) | set(got)
                if not _close(float(got.get(k, -1)), float(want.get(k, -2)))}
        ok = not diff and bool(got)
        return {"pass": ok, "reason": "各组计数一致" if ok else
                f"不一致：{'; '.join(f'{k} 期望{a} 得到{b}' for k, (a, b) in list(diff.items())[:4])}"}
    return {"pass": False, "reason": "未知题型"}


def run(questions: list[dict], ask_fn) -> dict:
    """ask_fn(question) -> qa.ask 结果。返回逐题结果与分题型汇总。"""
    items = []
    for q in questions:
        t0 = time.perf_counter()
        try:
            r = ask_fn(q["question"])
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        sc = score(q, r)
        items.append({**q, "answer": (r.get("answer") or r.get("error") or "")[:300],
                      "engine": r.get("engine"), "ms": round((time.perf_counter() - t0) * 1000),
                      **sc})
    by = {}
    for it in items:
        b = by.setdefault(it["kind"], {"total": 0, "pass": 0})
        b["total"] += 1
        b["pass"] += int(it["pass"])
    for b in by.values():
        b["rate"] = round(b["pass"] / b["total"], 3)
    n = len(items)
    p = sum(int(i["pass"]) for i in items)
    return {"total": n, "passed": p, "accuracy": round(p / n, 3) if n else 0.0, "by_kind": by, "items": items}
