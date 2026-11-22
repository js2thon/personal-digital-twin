"""⑥ AI 自助问答：问题 → 查询计划（AST）→ 校验 → 执行 → 答案 + 证据。

原则（与 cq_v2.planner 一致）：
- LLM 只产出**计划 AST**，不产出 SQL；AST 只能引用已登记的指标 / 维度 / 类 / 属性；
- 校验器白名单所有引用、限制 top_n / 过滤条件数量；
- 执行有两条路：内存（物化结果，`metrics.evaluate`）与 SQL（源库，`metrics.to_sql`，只读+超时）；
- LLM 不可用时用确定性规则解析（中文关键词 → 指标/维度/过滤/排序），保证离线可用。

AST:
{"op": "metric", "metric": "<id>", "dim": "<dim id>|null", "filters": [{"dim": id, "value": v}],
 "order": "desc|asc|null", "top_n": int|null, "grain": "month|year|null"}
{"op": "list", "cls": "<Class>", "filters": [...], "limit": int}
{"op": "lookup", "cls": "<Class>", "name": "<实体名>", "attributes": [...]}
"""
from __future__ import annotations

import json
import re

from . import metrics as mx
from .generator import SYNONYMS

MAX_FILTERS = 3
MAX_TOP_N = 100
ALLOWED_OPS = ("metric", "list", "lookup")

_COUNT = re.compile(r"(多少|几个|几位|几名|人数|数量|总数|how many|count)", re.I)
_AVG = re.compile(r"(平均|均值|人均|avg|average|mean)", re.I)
_SUM = re.compile(r"(总|合计|总和|累计|sum|total)", re.I)
_MAX = re.compile(r"(最高|最大|max)", re.I)
_BY = re.compile(r"(各|每个|按|分别|by|per)", re.I)
_TOP = re.compile(r"(前|top)\s*(\d+)", re.I)
_DESC = re.compile(r"(最多|最高|最大|排名|排行|降序|top)", re.I)
_ASC = re.compile(r"(最少|最低|最小|升序)", re.I)
_YEAR = re.compile(r"(每年|按年|年度|逐年|历年)")
_MONTH = re.compile(r"(每月|按月|月度|逐月)")
_LIST = re.compile(r"(有哪些|列出|名单|都有谁|清单|是谁|哪些人|list)", re.I)


# ------------------------------------------------------------------ 语义目录
def catalog(schema: dict, metric_set: dict) -> dict:
    """给 LLM 与规则解析共用的「可问什么」清单。"""
    ms = metric_set["metrics"]
    dims = {}
    for cls in schema["entity_types"]:
        for d in mx.dimensions(schema, cls):
            dims.setdefault(d["id"], d)
    return {
        "classes": {c: {"label": v.get("label") or c, "aliases": v.get("aliases") or []}
                    for c, v in schema["entity_types"].items()},
        "metrics": {k: {"label": m["label"], "kind": m["kind"], "cls": m.get("cls"),
                        "dims": m.get("dims_allowed") or []}
                    for k, m in ms.items() if m["kind"] != "derived" or m.get("grain")},
        "dimensions": {k: {"label": d["label"], "class": d["class"], "attribute": d["attribute"]}
                       for k, d in dims.items()},
    }


# ------------------------------------------------------------------ 规则解析（离线兜底）
def _terms(s: str) -> list[str]:
    return [t for t in re.split(r"[\s/·→.，,、\-（）()]+", s or "") if t]


def _score_label(q: str, label: str) -> int:
    """整串命中按长度×2；否则按 ≥2 字的连续片段命中累计（中文无空格分词）。"""
    label = str(label or "")
    if len(label) < 2:
        return 0
    if label in q:
        return len(label) * 2
    best = 0
    for t in _terms(label):
        for n in range(min(len(t), 6), 1, -1):
            hit = next((t[i:i + n] for i in range(len(t) - n + 1) if t[i:i + n] in q), None)
            if hit:
                best += n
                break
    return best


def _class_words(schema: dict, cls: str) -> list[str]:
    v = schema["entity_types"].get(cls) or {}
    words = [v.get("label") or cls, cls, *(v.get("aliases") or []), *SYNONYMS.get(cls, ())]
    return [w for w in dict.fromkeys(map(str, words)) if w]


def _variants(label: str, schema: dict) -> list[str]:
    """把标签里的类中文名替换成同义词：「人员数」→「员工数」「成员数」。"""
    out = [label]
    for cls, v in schema["entity_types"].items():
        lab = v.get("label") or cls
        if lab and lab in label:
            for w in _class_words(schema, cls):
                if re.search(r"[\u4e00-\u9fff]", w):
                    out.append(label.replace(lab, w))
    # 「人员数」≈「人数」「多少人」；「平均每组织单元人员数」≈「每个部门平均多少人」
    for v in list(out):
        out.append(re.sub(r"(人员|员工|成员)数", "人数", v))
    return list(dict.fromkeys(out))


def _best(q: str, labels: list[str], schema: dict) -> int:
    return max((_score_label(q, v) for lab in labels for v in _variants(str(lab), schema)), default=0)


def _class_in(q: str, schema: dict) -> str | None:
    best, sc = None, 0
    for c in schema["entity_types"]:
        s = max((_score_label(q, w) if len(w) >= 2 else (2 if w in q else 0))
                for w in _class_words(schema, c))
        if s > sc:
            best, sc = c, s
    return best


def _value_filters(q: str, schema: dict, F: dict | None) -> list[dict]:
    """问题里出现的枚举/名称取值 → 过滤条件（如「研发部」→ works_at__dept_name=研发部）。"""
    if not F:
        return []
    out = []
    for cls in schema["entity_types"]:
        for d in mx.dimensions(schema, cls):
            tgt = F.get(d["class"])
            if tgt is None or d["attribute"] not in tgt:
                continue
            for v in tgt[d["attribute"]].dropna().astype(str).unique()[:500]:
                nv = re.sub(r"\s*[（(][^（）()]*[）)]\s*", "", v).strip()   # 「刘涛 (Liu Tao)」→「刘涛」
                if len(nv) >= 2 and nv in q:
                    out.append({"dim": d["id"], "value": v, "cls": cls})
    return out


def rule_parse(question: str, schema: dict, metric_set: dict, F: dict | None = None) -> dict:
    q = question.strip()
    ms = metric_set["metrics"]
    cls = _class_in(q, schema)
    vf = _value_filters(q, schema, F)

    # list：有哪些 X
    if _LIST.search(q) and cls and not _COUNT.search(q):
        plan = {"op": "list", "cls": cls,
                "filters": [{"dim": f["dim"], "value": f["value"]} for f in vf if f["cls"] == cls][:MAX_FILTERS],
                "limit": 50}
        tf = _time_filter(q, schema, cls)
        if tf:
            plan["time_filter"] = tf
        return plan

    # 指标：先按标签相似度挑，再按聚合关键词纠偏
    def agg_bonus(m):
        b = 0
        if m["kind"] == "composite":
            b += 4 if re.search(r"(人均|每|占比|比例|幅度|平均每)", q) else -2
        if m.get("agg") == "count":
            # 「平均年龄是多少」里的「多少」不是计数；问到聚合词时计数指标降权
            b += (-3 if _AVG.search(q) or _SUM.search(q) or _MAX.search(q) else 3) if _COUNT.search(q) else 0
        if m.get("agg") == "avg":
            b += -2 if re.search(r"(人均|每个?[^\s]{1,4}平均|平均每)", q) else (3 if _AVG.search(q) else -1)
        if m.get("agg") == "sum":
            b += 2 if _SUM.search(q) else -1
        if m.get("agg") == "max":
            b += 2 if _MAX.search(q) and not _BY.search(q) else -2
        if cls and m.get("cls") == cls:
            b += 2
        return b

    # 过滤取值（如「数据管理部」）不参与指标匹配：否则会被类名「数据管理部人员」吸走，误选计数指标
    q_full, q = q, q
    for f in sorted(vf, key=lambda f: -len(str(f["value"]))):
        nv = re.sub(r"\s*[（(][^（）()]*[）)]\s*", "", str(f["value"])).strip()
        if len(nv) >= 2:
            q = q.replace(nv, " ")
    cands = []
    for mid, m in ms.items():
        if m["kind"] == "derived":
            continue
        text = _best(q, [m["label"]], schema)
        if any(v in q for v in _variants(m["label"], schema)):
            text += 6                                    # 指标全名直接出现
        if m.get("measure"):
            alab = schema["attributes"][m["measure"]].get("label") or m["measure"]
            text += _score_label(q, alab)
        if text <= 0:
            continue
        cands.append((text + agg_bonus(m), text, mid))
    cands.sort(reverse=True)
    if not cands and cls:
        mid = f"{mx._snake(cls)}_count"
        if mid in ms:
            cands = [(1, 1, mid)]
    if not cands:
        return {"op": "unknown", "reason": "没有在本体中找到与问题相关的指标或对象"}
    cands = [(a, c) for a, _, c in cands]
    # 「X 有多少人 / 多少个 Y」且没有更具体的指标命中 → 该类计数
    if cls and _COUNT.search(q) and f"{mx._snake(cls)}_count" in ms \
            and not (_AVG.search(q) or _SUM.search(q) or _MAX.search(q)):
        top = ms[cands[0][1]]
        if top.get("agg") not in ("count", "count_links") and top["kind"] != "composite":
            cands.insert(0, (99, f"{mx._snake(cls)}_count"))
    # 「每个 B 平均多少 A」→ A_per_B
    mper = re.search(r"每个?(\S{1,6}?)平均(有)?多少(\S{0,4})", q)
    if mper:
        b = _class_in(mper.group(1), schema)
        a = _class_in(mper.group(3) or "人", schema) or cls
        key = f"{mx._snake(a)}_per_{mx._snake(b)}" if a and b else None
        if key in ms:
            cands.insert(0, (99, key))
    mid = cands[0][1]
    m = ms[mid]
    q = q_full

    # 时间粒度 → 派生
    grain = "year" if _YEAR.search(q) else "month" if _MONTH.search(q) else None
    if grain:
        did = f"{mid}_by_{grain}"
        if did in ms:
            return {"op": "metric", "metric": did, "dim": None, "filters": [], "order": None,
                    "top_n": None, "grain": grain}

    tm = _TOP.search(q)
    qo = q
    for v in _variants(m["label"], schema):       # 「最高薪资」里的「最高」属于指标名，不是排序
        qo = qo.replace(v, "")
    order = "desc" if _DESC.search(qo) else "asc" if _ASC.search(qo) else None
    # 维度：问题中「按/各/每个」+ 维度标签；或出现取值时以取值所在维度过滤
    allowed = m.get("dims_allowed") or []
    dim = None
    qb = re.sub(r"每个?\S{1,6}?平均", "", qo)       # 「每个部门平均」是指标口径，不是分组
    if _BY.search(qb) or order:
        best = 0
        for did in allowed:
            d = _dim(schema, m, did, metric_set)
            attr_lab = schema["attributes"].get(d["attribute"], {}).get("label") or d["attribute"]
            labs = [attr_lab, *(d.get("aliases") or [])]
            if d["path"]:
                labs += _class_words(schema, d["class"])
            s = _best(q, labs, schema) - len(d["path"]) * 0.1   # 同分取更近的维度
            if s > best:
                best, dim = s, did
    seen_v, filters = set(), []
    def _cover(f):                                   # 同值多列：取覆盖行数最多的维度（层级部门取上级列）
        try:
            d = _dim(schema, m, f["dim"], metric_set)
            col = F[d["class"]][d["attribute"]].astype(str)
            return -int((col == str(f["value"])).sum())
        except Exception:  # noqa: BLE001
            return 0
    for f in sorted((f for f in vf if f["dim"] in allowed), key=lambda f: (f["dim"].count("__"), _cover(f))):
        if f["value"] not in seen_v:                    # 同一取值只取最近的维度（P7 → level，而非 reports_to__level）
            seen_v.add(f["value"])
            filters.append({"dim": f["dim"], "value": f["value"]})
    filters = filters[:MAX_FILTERS]
    tf = _time_filter(q, schema, mx._root(m, ms)["cls"])
    if order and not dim and allowed and not filters:
        dim = allowed[0]
    out = {"op": "metric", "metric": mid, "dim": dim, "filters": filters,
            "order": order, "top_n": int(tm.group(2)) if tm else (1 if order and re.search(
                r"(最多|最少|最高|最低|哪个|哪位)", qo) else None),
            "grain": None}
    if tf:
        out["time_filter"] = tf
    return out


_TIME_CMP = re.compile(r"(\d{4})\s*年?\s*(以后|之后|后|以来|起|以前|之前|前|当年|年内|年)?")


def _time_filter(q: str, schema: dict, cls: str) -> dict | None:
    """「2023年后入职」→ {attr: hire_date, op: '>=', value: '2023-01-01'}。"""
    m = _TIME_CMP.search(q)
    if not m:
        return None
    attrs = mx.time_columns(schema, cls)
    if not attrs:
        return None
    attr = max(attrs, key=lambda a: _score_label(q, schema["attributes"][a].get("label") or a))
    y, w = int(m.group(1)), m.group(2) or "年"
    if w in ("以后", "之后", "后", "以来", "起"):
        return {"attr": attr, "op": ">=", "value": f"{y}-01-01"}
    if w in ("以前", "之前", "前"):
        return {"attr": attr, "op": "<", "value": f"{y}-01-01"}
    return {"attr": attr, "op": "between", "value": [f"{y}-01-01", f"{y + 1}-01-01"]}


def _dim(schema, m, did, metric_set):
    frame = m.get("frame") or m.get("cls") or mx._root(m, metric_set["metrics"])["cls"]
    for d in mx.dimensions(schema, frame):
        if d["id"] == did:
            return d
    raise KeyError(did)


# ------------------------------------------------------------------ LLM 解析
_SYSTEM = """你是数据问答的查询规划器。只输出 JSON 计划，不输出 SQL。
可用 op：metric（计算指标）、list（列出对象）、lookup（查某个对象的属性）。
metric 计划字段：metric, dim(可空), filters[{dim,value}], order(desc/asc/null), top_n, grain(month/year/null)。
只能使用目录里出现的 metric id、dimension id、class 名；dim 必须在该指标的 dims 列表中。
目录里没有能回答的指标时输出 {"op":"unknown","reason":"..."}。"""


def llm_parse(question: str, cat: dict, llm) -> dict | None:
    try:
        data = llm.chat_json(_SYSTEM, json.dumps({"catalog": cat, "question": question},
                                                 ensure_ascii=False), timeout=45)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) and data.get("op") else None


# ------------------------------------------------------------------ 校验
def validate_plan(plan: dict, schema: dict, metric_set: dict) -> list[str]:
    errs = []
    op = plan.get("op")
    ms = metric_set["metrics"]
    if op not in ALLOWED_OPS:
        return [f"不支持的 op：{op}"]
    for k in ("sql", "raw_sql", "query", "cypher", "sparql"):
        if k in plan:
            errs.append(f"计划不得包含 {k}")
    filters = plan.get("filters") or []
    if len(filters) > MAX_FILTERS:
        errs.append(f"过滤条件最多 {MAX_FILTERS} 个")
    if op == "metric":
        m = ms.get(plan.get("metric"))
        if not m:
            return errs + [f"未登记的指标：{plan.get('metric')}"]
        allowed = set(m.get("dims_allowed") or [])
        if plan.get("dim") and plan["dim"] not in allowed:
            errs.append(f"维度 {plan['dim']} 不适用于指标 {m['id']}")
        for f in filters:
            if f.get("dim") not in allowed:
                errs.append(f"过滤维度 {f.get('dim')} 不适用于指标 {m['id']}")
        tn = plan.get("top_n")
        if tn is not None and not (isinstance(tn, int) and 1 <= tn <= MAX_TOP_N):
            errs.append("top_n 需在 1～100")
        if plan.get("order") not in (None, "asc", "desc"):
            errs.append("order 只能是 asc/desc")
    tf = plan.get("time_filter")
    if tf:
        if tf.get("attr") not in schema["attributes"] or tf.get("op") not in (">=", "<", "between"):
            errs.append("时间过滤不合法")
    if op in ("list", "lookup") and plan.get("cls") not in schema["entity_types"]:
        errs.append(f"未登记的类：{plan.get('cls')}")
    if op == "lookup":
        for a in plan.get("attributes") or []:
            if a not in schema["attributes"]:
                errs.append(f"未登记的属性：{a}")
    return errs


# ------------------------------------------------------------------ 执行（内存）
def _filtered_frames(F, schema, m, filters, metric_set):
    """在指标参照系类上应用过滤：保留满足条件的实例，并把过滤传导到依赖类。"""
    if not filters:
        return F
    import pandas as pd
    F2 = dict(F)
    frame = m.get("frame") or mx._root(m, metric_set["metrics"])["cls"]
    keep_frame = None
    for f in filters:
        d = _dim(schema, m, f["dim"], metric_set)
        tmp = mx._attach_dim(F[frame], d, F, schema)
        ids = set(tmp.loc[tmp["dim"].astype(str) == str(f["value"]), "_id"])
        keep_frame = ids if keep_frame is None else keep_frame & ids
    F2[frame] = F[frame][F[frame]["_id"].isin(keep_frame)]
    # 依赖类：沿关系落到被保留的 frame 实例上
    for dep in m.get("deps") or []:
        dcls = mx._root(metric_set["metrics"][dep], metric_set["metrics"])["cls"]
        if dcls == frame:
            continue
        path = mx._path(schema, dcls, frame) or []
        if not path:
            continue
        cur = F[dcls][["_id", path[0]]].rename(columns={path[0]: "_k"})
        for rn in path[1:]:
            prev = schema["relation_types"][rn]["source"]
            cur = cur.merge(F[prev][["_id", rn]].rename(columns={"_id": "_k", rn: "_k2"}),
                            on="_k", how="left").drop(columns="_k").rename(columns={"_k2": "_k"})
        F2[dcls] = F[dcls][F[dcls]["_id"].isin(set(cur.loc[cur["_k"].isin(keep_frame), "_id"]))]
    return F2


def _records(df) -> list[dict]:
    """DataFrame → JSON 安全的记录：NaN/NaT/numpy 标量转成 None / 原生类型（JSON 不允许 NaN）。"""
    import math
    out = []
    for r in df.to_dict(orient="records"):
        row = {}
        for k, v in r.items():
            if hasattr(v, "item"):
                v = v.item()
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                v = None
            elif v is not None and type(v).__name__ in ("NaTType", "Timestamp"):
                v = None if str(v) == "NaT" else str(v)
            row[k] = v
        out.append(row)
    return out


def _apply_time(df, tf):
    import pandas as pd
    s = pd.to_datetime(df[tf["attr"]], errors="coerce")
    if tf["op"] == ">=":
        return df[s >= pd.Timestamp(tf["value"])]
    if tf["op"] == "<":
        return df[s < pd.Timestamp(tf["value"])]
    lo, hi = tf["value"]
    return df[(s >= pd.Timestamp(lo)) & (s < pd.Timestamp(hi))]


def execute(plan: dict, schema: dict, metric_set: dict, F: dict, L: dict) -> dict:
    import pandas as pd
    ms = metric_set["metrics"]
    if plan["op"] == "list":
        df = F[plan["cls"]]
        for f in plan.get("filters") or []:
            d = next(x for x in mx.dimensions(schema, plan["cls"]) if x["id"] == f["dim"])
            tmp = mx._attach_dim(df, d, F, schema)
            df = df[df["_id"].isin(set(tmp.loc[tmp["dim"].astype(str) == str(f["value"]), "_id"]))]
        if plan.get("time_filter"):
            df = _apply_time(df, plan["time_filter"])
        rows = df.head(plan.get("limit") or 50)
        rows = rows.drop(columns=[c for c in rows.columns if c.startswith("_") and c != "_name"])
        return {"kind": "list", "total": int(len(df)), "rows": _records(rows)}
    if plan["op"] == "lookup":
        df = F[plan["cls"]]
        hit = df[df["_name"].astype(str) == str(plan.get("name"))]
        cols = plan.get("attributes") or [c for c in df.columns if not c.startswith("_")]
        return {"kind": "lookup", "rows": _records(hit[["_name", *[c for c in cols if c in hit]]])}
    m = ms[plan["metric"]]
    F2 = _filtered_frames(F, schema, m, plan.get("filters"), metric_set)
    tf = plan.get("time_filter")
    if tf:
        owner = schema["attributes"][tf["attr"]]["owner"]
        F2 = dict(F2)
        F2[owner] = _apply_time(F2[owner], tf)
    val = mx.evaluate(plan["metric"], ms, F2, L, schema, plan.get("dim"))
    if isinstance(val, pd.Series):
        s = val.dropna()
        if plan.get("order"):                          # 排名时空值分组不参与（「(空)」不是一个业务类别）
            s = s[~s.index.astype(str).isin(["(空)", "", "nan", "None"])]
            s = s.sort_values(ascending=plan["order"] == "asc")
        if plan.get("top_n"):
            s = s.head(plan["top_n"])
        return {"kind": "series", "index": [str(i) for i in s.index],
                "values": [float(v) for v in s.values]}
    import math
    v = None if val is None else float(val)
    return {"kind": "scalar", "value": None if v is None or math.isnan(v) or math.isinf(v) else v}


# ------------------------------------------------------------------ 回答
def _fmt(v):
    if v is None:
        return "—"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def answer_text(plan: dict, res: dict, schema: dict, metric_set: dict) -> str:
    if plan["op"] == "list":
        names = [r.get("_name") for r in res["rows"]][:10]
        lab = schema["entity_types"][plan["cls"]].get("label") or plan["cls"]
        return f"共 {res['total']} 个{lab}：" + "、".join(map(str, names)) + ("…" if res["total"] > 10 else "")
    if plan["op"] == "lookup":
        return "；".join(json.dumps(r, ensure_ascii=False, default=str) for r in res["rows"]) or "未找到该对象"
    m = metric_set["metrics"][plan["metric"]]
    _vals = [str(f["value"]) for f in plan.get("filters") or []]
    scope = "，".join(v for v in dict.fromkeys(_vals) if not any(v != w and v in w for w in _vals))
    tf = plan.get("time_filter")
    if tf:
        lab = schema["attributes"][tf["attr"]].get("label") or tf["attr"]
        t = {">=": f"{tf['value'][:4]}年起", "<": f"{tf['value'][:4]}年以前"}.get(
            tf["op"], f"{str(tf['value'][0])[:4]}年")
        scope = "，".join(x for x in (scope, f"{lab}{t}") if x)
    head = f"{scope}的" if scope else ""
    if res["kind"] == "scalar":
        return f"{head}{m['label']}为 {_fmt(res['value'])}{m.get('unit') or ''}"
    pairs = list(zip(res["index"], res["values"]))
    if not pairs:
        return f"{head}{m['label']}：没有数据"
    if plan.get("top_n") == 1:
        k, v = pairs[0]
        return f"{head}{m['label']}{'最高' if plan.get('order') != 'asc' else '最低'}的是 {k}（{_fmt(v)}）"
    return f"{head}{m['label']}：" + "；".join(f"{k} {_fmt(v)}" for k, v in pairs[:12])


def chart_spec(plan: dict, res: dict, metric_set: dict) -> dict | None:
    """前端可直接渲染的最小图表规格：时间序列→折线，维度→条形。"""
    if res.get("kind") != "series":
        return None
    m = metric_set["metrics"][plan["metric"]]
    typ = "line" if m["kind"] == "derived" else "bar"
    return {"type": typ, "title": m["label"], "categories": res["index"], "data": res["values"]}


def _source_evidence(contract: dict | None, schema: dict, plan: dict | None,
                     result: dict | None) -> dict:
    """Attach deterministic source mapping evidence; never imply discovery samples are production data."""
    mappings = (contract or {}).get("mappings") or []
    by_class = {m.get("class"): m for m in mappings if m.get("class")}
    refs = []
    classes = set()
    if isinstance(plan, dict) and plan.get("cls"):
        classes.add(plan["cls"])
    if isinstance(result, dict):
        for row in result.get("rows") or []:
            if row.get("_id"):
                refs.append({"instance_id": str(row["_id"])})
        for row in result.get("entities") or []:
            if row.get("id"):
                refs.append({"instance_id": str(row["id"])})
                if row.get("class"):
                    classes.add(row["class"])
        for row in result.get("items") or []:
            for member in row.get("members") or []:
                if isinstance(member, dict) and member.get("id"):
                    refs.append({"instance_id": str(member["id"])})
    source_map = []
    for cls in sorted(classes):
        mapping = by_class.get(cls)
        if not mapping:
            continue
        source_map.append({
            "entity_type": cls,
            "source": (contract or {}).get("source"),
            "table": mapping.get("table"),
            "primary_key": mapping.get("primary_key") or [],
            "property_columns": dict(mapping.get("attributes") or {}),
            "relation_columns": [
                {"relation": rel.get("type"), "column": rel.get("column"),
                 "target_table": rel.get("target_table")}
                for rel in mapping.get("relations") or []
            ],
        })
    unique_refs = list({r["instance_id"]: r for r in refs}.values())[:50]
    return {
        "data_scope": "discovery_sample",
        "scope_note": "当前 Automodel 问答基于本体建模阶段的 Discovery 样本，不代表源库全量统计。",
        "source_mappings": source_map,
        "matched_instances": unique_refs,
        "traceability_level": "source_table_and_column",
    }


# ------------------------------------------------------------------ 主入口
def ask(question: str, schema: dict, metric_set: dict, F: dict, L: dict,
        llm=None, contract: dict | None = None) -> dict:
    """本体问答主入口。

    顺序：① 本体图检索（实体/属性/分组/过滤，graphqa）→ ② 指标计划（LLM 或规则）→ ③ 关键词检索兜底；
    所有分支的结果都作为「依据」交给 LLM 做依据式作答（不可用时模板回答），并原样返回依据。
    """
    from . import graphqa as gq
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "问题为空"}
    idx = gq.Index(schema, F, L)
    brief = gq.schema_brief(schema, F)

    # ① 本体图检索：实体档案 / 属性 / 分组 / 过滤
    g = gq.retrieve(question, schema, F, L, idx)
    if g:
        ans = gq.grounded_answer(question, {"本体结构": brief, "检索结果": g}, llm) if llm else None
        evidence = {"ontology_result": g,
                    "provenance": _source_evidence(contract, schema, {"op": g.get("op")}, g)}
        return {"ok": True, "engine": "ontology_graph" + ("+llm" if ans else ""), "plan": {"op": g["op"]},
                "result": g, "answer": ans or gq.template_answer(g), "chart": gq.chart_of(g),
                "evidence": evidence}

    # ② 指标计划
    out = _metric_ask(question, schema, metric_set, F, L, llm, contract)
    if out.get("ok"):
        if llm:
            ans = gq.grounded_answer(question, {"本体结构": brief, "指标": out.get("metric"),
                                                "计划": out["plan"], "计算结果": out["result"]}, llm)
            if ans:
                out["template_answer"], out["answer"] = out["answer"], ans
                out["engine"] += "+llm"
        out["evidence"] = {
            "metric": out.get("metric"), "result": out["result"],
            "provenance": _source_evidence(contract, schema, out.get("plan"), out.get("result")),
        }
        return out

    # ③ 兜底：关键词检索相关实例，交给 LLM 依据作答
    rows = gq.keyword_rows(question, idx)
    if rows and llm:
        ans = gq.grounded_answer(question, {"本体结构": brief, "相关实例": rows}, llm)
        if ans:
            return {"ok": True, "engine": "ontology_retrieval+llm", "plan": {"op": "retrieve"},
                    "result": {"kind": "list", "total": len(rows), "rows": rows},
                    "answer": ans, "evidence": {"rows": rows}}
    if rows:
        # 无 LLM 时不臆测：没有指标/属性/关系能回答，只给出可能相关的实例作为线索
        return {"ok": False, "engine": "ontology_retrieval",
                "error": "本体中没有能直接回答该问题的指标、属性或关系",
                "related": [r["name"] for r in rows[:10]],
                "suggestions": suggest(schema, metric_set)}
    return out


def _metric_ask(question: str, schema: dict, metric_set: dict, F: dict, L: dict,
                llm=None, contract: dict | None = None) -> dict:
    cat = catalog(schema, metric_set)
    attempts = []
    plan, engine = None, None
    if llm is not None:
        p = llm_parse(question, cat, llm)
        if p:
            errs = validate_plan(p, schema, metric_set)
            attempts.append({"engine": "llm", "plan": p, "errors": errs})
            if not errs:
                plan, engine = p, "llm"
    if plan is None:
        p = rule_parse(question, schema, metric_set, F)
        if p.get("op") == "unknown":
            return {"ok": False, "error": p.get("reason"), "attempts": attempts,
                    "suggestions": suggest(schema, metric_set)}
        errs = validate_plan(p, schema, metric_set)
        attempts.append({"engine": "rule", "plan": p, "errors": errs})
        if errs:
            return {"ok": False, "error": "；".join(errs), "plan": p, "attempts": attempts}
        plan, engine = p, "rule"
    res = execute(plan, schema, metric_set, F, L)
    out = {"ok": True, "engine": engine, "plan": plan, "result": res,
           "answer": answer_text(plan, res, schema, metric_set),
           "chart": chart_spec(plan, res, metric_set), "attempts": attempts}
    if plan["op"] == "metric":
        m = metric_set["metrics"][plan["metric"]]
        out["metric"] = {"id": m["id"], "label": m["label"], "kind": m["kind"],
                         "expr": m.get("expr"), "deps": m.get("deps")}
        if contract and not plan.get("filters"):
            try:
                out["sql"] = mx.to_sql(plan["metric"], metric_set["metrics"], schema, contract, plan.get("dim"))
            except (NotImplementedError, KeyError, StopIteration, ValueError):
                pass
    return out


def suggest(schema: dict, metric_set: dict, n: int = 8) -> list[str]:
    """按本体与指标生成推荐问题。"""
    ms = metric_set["metrics"]
    out = []
    for m in ms.values():
        if m["kind"] == "derived":
            continue
        dims = m.get("dims_allowed") or []
        if dims and m["kind"] != "derived":
            d = _dim(schema, m, dims[-1], metric_set)
            out.append(f"按{d['label']}统计{m['label']}")
        else:
            out.append(f"{m['label']}是多少？")
        if len(out) >= n:
            break
    from . import graphqa as gq
    lead = []
    for cls, v in schema["entity_types"].items():
        lab = v.get("label") or cls
        groupable = [a for a, m in schema["attributes"].items() if m["owner"] == cls
                     and m.get("role") in ("enum", "name") and gq.attr_words(schema, a)]
        groupable.sort(key=lambda a: -gq._attr_rank(schema, a))
        for a in groupable[:3]:
            lead.append(f"{lab}的{gq.attr_words(schema, a)[0]}分布")
        lead.append(f"有哪些{lab}？")
    return (lead + out)[:n + 2]


# ------------------------------------------------------------------ SQL 执行（源库直连，只读）
def run_sql(url: str, sql: str, timeout_ms: int = 15000, max_rows: int = 1000) -> dict:
    """在源库上执行 to_sql 生成的只读 SQL。只允许单条 SELECT。"""
    from sqlalchemy import create_engine, text

    s = sql.strip().rstrip(";")
    if not re.match(r"(?is)^select\s", s) or ";" in s:
        raise ValueError("只允许单条 SELECT")
    eng = create_engine(url)
    with eng.connect() as conn:
        if eng.dialect.name == "postgresql":
            conn.execute(text(f"SET statement_timeout = {int(timeout_ms)}"))
            conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = conn.execute(text(s)).mappings().fetchmany(max_rows)
    eng.dispose()
    return {"rows": [dict(r) for r in rows]}
