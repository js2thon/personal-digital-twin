"""⑦ 基于本体的问答（图谱检索 + 确定性计算 + LLM 依据式作答）。

参考 ontology_studio_v2 的思路——「把本体结构和业务对象交给 LLM，严格依据本体回答」——
但不把全量数据塞进上下文，而是先在本体图上**检索**与问题相关的子图：

1. 实体链接：问题里出现的实体名（姓名去掉英文括注、部门名、项目名…）→ 实例；
2. 属性链接：「岗位/部门/职级/学历…」等中文说法 → 本体属性（优先 *_cn_name）；
3. 确定性答案（可复核）：
   - lookup：某实体的某属性 / 档案 + 一跳邻居（经关系连到的实体）；
   - group：按某属性分组计数（「各学历人数」「男女比例」「岗位分布」）；
   - filter_count / filter_list：按属性取值过滤后计数或列名单（「数据合规组有多少人」）；
   - 其余交给指标问答（qa.rule_parse / 计划 AST）；
4. LLM 作答：把「本体结构摘要 + 命中的实体与三元组 + 确定性结果」作为唯一依据，
   要求只用依据回答并标注引用；LLM 不可用时用模板回答。答案里不会出现依据之外的数据。
"""
from __future__ import annotations

import json
import math
import re

MAX_CONTEXT_ENTITIES = 30
MAX_CONTEXT_ROWS = 60
GROUP_MAX_DISTINCT = 60

_AGG_STRICT = re.compile(r"(平均|均值|合计|总和|总计|累计|最高|最低|最大|最小|人均|幅度|薪|预算|金额)")
_AGG = re.compile(r"(平均|均值|合计|总和|总计|累计|最高|最低|最大|最小|人均|占比|比例)")
_PAREN = re.compile(r"\s*[（(][^（）()]*[）)]\s*")

# 列名词根 → 中文业务说法（HR / 组织 / 项目常见字段；可在本体 label 里覆盖）
ATTR_WORDS = [
    (r"post|position(?!_entry)", ["岗位", "职位"]),
    (r"^dept(_cn)?_name$|^dept_cn_name$|department", ["部门", "所在部门", "所属部门"]),
    (r"dept_manager", ["部门负责人", "部门经理", "负责人", "领导"]),
    (r"manager|leader|supervisor|reports_to|line_rep", ["上级", "汇报", "主管", "直属领导", "下属", "下级", "团队", "带了", "带几"]),
    (r"owner|owned_by", ["负责人", "负责", "所有者", "牵头"]),
    (r"member", ["参与", "成员", "参加"]),
    (r"part_of|parent", ["上级部门", "隶属", "所属"]),
    (r"works_at|^dept_id$", ["部门", "任职", "在哪"]),
    (r"grade|level|pos_grd", ["职级", "级别", "等级"]),
    (r"^job", ["职务", "职位序列"]),
    (r"educ", ["学历", "教育", "最高学历"]),
    (r"sex|gender", ["性别", "男女"]),
    (r"^age$", ["年龄", "岁"]),
    (r"birthdate|birth_date|birthday", ["出生日期", "生日"]),
    (r"birthplace|birthstate|birthcountry", ["籍贯", "出生地"]),
    (r"nationality", ["国籍", "民族"]),
    (r"marital", ["婚姻", "婚否"]),
    (r"political", ["政治面貌", "党员"]),
    (r"hire|entry_dt|join", ["入职", "入职日期", "入职时间"]),
    (r"start", ["开始", "启动", "开工"]),
    (r"end_date|finish", ["结束", "完成"]),
    (r"location|city", ["工作地", "地点", "城市", "办公地"]),
    (r"company", ["公司", "法人", "签约公司"]),
    (r"region", ["区域", "大区"]),
    (r"employment_type|empl_class|employee_type|work_type", ["用工类型", "员工类别", "员工类型"]),
    (r"officer", ["干部"]),
    (r"status", ["状态", "在职"]),
    (r"salary|pay", ["薪资", "工资", "薪酬"]),
    (r"budget", ["预算"]),
    (r"emplid|emp_no|employee_id", ["工号", "员工号", "编号"]),
    (r"address", ["地址", "住址"]),
    (r"^name$|full_name|emp_name", ["姓名", "名字"]),
]
_GROUP = re.compile(r"(各|每个|每种|按|分布|构成|占比|比例|分别|多少种|哪些.{0,4}(类|种))")
_COUNT = re.compile(r"(多少|几个|几位|几名|人数|数量|总数|how many)", re.I)
_LIST = re.compile(r"(有哪些|都有谁|名单|列出|是谁|哪些人|成员)")


def _norm(v) -> str:
    return _PAREN.sub("", str(v)).strip()


def _finite(v):
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


# ------------------------------------------------------------------ 属性词典
def attr_words(schema: dict, attr: str) -> list[str]:
    meta = schema["attributes"].get(attr) or {}
    col = str(meta.get("column") or attr).lower()
    words = {meta.get("label") or "", meta.get("description") or ""}
    for pat, ws in ATTR_WORDS:
        if re.search(pat, col):
            words.update(ws)
    return [w for w in words if w and len(w) >= 2 and not re.fullmatch(r"[a-z0-9_]+", w)]


def _attr_rank(schema: dict, attr: str) -> int:
    """同义属性择优：中文名 > 名称 > 枚举 > 编码 > 英文名。"""
    col = (schema["attributes"][attr].get("column") or attr).lower()
    role = schema["attributes"][attr].get("role")
    s = 0
    if "_cn_" in col or col.endswith("_cn_name") or col.endswith("_name") and "_en_" not in col:
        s += 4
    if "_en_" in col or col.endswith("_en_name"):
        s -= 4
    if col.endswith("_code") or role == "identifier":
        s -= 2
    if re.search(r"_lv\d+$|_end$", col):          # 多级部门明细列，次选
        s -= 1
    if role in ("name", "enum"):
        s += 1
    return s


def match_attrs(q: str, schema: dict, cls: str | None = None) -> list[str]:
    """问题中提到的属性（按命中长度 + 择优排序）。"""
    hits = []
    for a, m in schema["attributes"].items():
        if cls and m.get("owner") != cls:
            continue
        best = max((len(w) for w in attr_words(schema, a) if w in q), default=0)
        if best:
            hits.append((best, _attr_rank(schema, a), a))
    hits.sort(reverse=True)
    out, seen_words = [], set()
    for _, _, a in hits:
        key = tuple(sorted(w for w in attr_words(schema, a) if w in q))
        if key in seen_words:
            continue                                  # 同一说法只取最优列
        seen_words.add(key)
        out.append(a)
    return out


# ------------------------------------------------------------------ 图索引
class Index:
    """把物化帧变成可检索的本体图：实例、名称索引、边（多对一列 + 多对多关联）。"""

    def __init__(self, schema: dict, F: dict, L: dict):
        self.schema, self.F, self.L = schema, F, L
        self.ent: dict[str, dict] = {}
        self.names: dict[str, list[str]] = {}
        self.out_edges: dict[str, list[tuple[str, str]]] = {}
        self.in_edges: dict[str, list[tuple[str, str]]] = {}
        rels = schema["relation_types"]
        for cls, df in F.items():
            for r in df.to_dict(orient="records"):
                eid = r["_id"]
                self.ent[eid] = {"cls": cls, "row": r}
                for key in {_norm(r.get("_name") or ""), str(r.get("_name") or "")}:
                    # 单字名（如「甲」）也入索引，但只在问题里作为独立词出现时才链接（见 link）
                    if len(key) >= 1 and not key.isdigit():
                        self.names.setdefault(key, []).append(eid)
        for rn, spec in rels.items():
            if spec.get("cardinality") == "many_to_many":
                for e in (L.get(rn).to_dict(orient="records") if L.get(rn) is not None else []):
                    self._edge(e["source"], rn, e["target"])
            else:
                for eid, e in self.ent.items():
                    if e["cls"] == spec.get("source"):
                        tgt = e["row"].get(rn)
                        if isinstance(tgt, str) and tgt in self.ent:
                            self._edge(eid, rn, tgt)

    def _edge(self, s, r, t):
        self.out_edges.setdefault(s, []).append((r, t))
        self.in_edges.setdefault(t, []).append((r, s))

    def name(self, eid: str) -> str:
        e = self.ent.get(eid)
        return _norm(e["row"].get("_name")) if e else eid

    def strip(self, q: str) -> str:
        """去掉问题里的实体名，避免「员工5」里的「员工」被当成关系/类词。"""
        for nm in sorted(self.names, key=len, reverse=True):
            q = q.replace(nm, " ")
        return q

    def link(self, q: str) -> list[str]:
        """实体链接：最长名称优先，且不允许被更长的命中覆盖。"""
        found, spans = [], []
        for nm in sorted(self.names, key=len, reverse=True):
            i = q.find(nm)
            if i < 0 or any(a <= i < b for a, b in spans):
                continue
            if len(nm) == 1 and not (i == 0 and re.match(r"(的|在|是|有|负责|参与|带|属于)", q[1:])):
                continue                              # 单字名只认句首主语，避免误链
            spans.append((i, i + len(nm)))
            found.extend(self.names[nm])
        return list(dict.fromkeys(found))

    def neighbors(self, eid: str) -> list[dict]:
        rl = {k: v.get("label") or k for k, v in self.schema["relation_types"].items()}
        out = [{"relation": rl.get(r, r), "direction": "out", "entity": self.name(t), "id": t}
               for r, t in self.out_edges.get(eid, [])]
        out += [{"relation": rl.get(r, r), "direction": "in", "entity": self.name(s), "id": s}
                for r, s in self.in_edges.get(eid, [])]
        return out

    def profile(self, eid: str, attrs: list[str] | None = None) -> dict:
        e = self.ent[eid]
        lab = {a: (self.schema["attributes"][a].get("label") or a) for a in self.schema["attributes"]}
        row = {k: _finite(v) for k, v in e["row"].items() if not k.startswith("_")}
        keep = attrs or [a for a in row if a in self.schema["attributes"] and row.get(a) is not None
                         and _attr_rank(self.schema, a) >= 0]
        return {"id": eid, "class": e["cls"], "name": self.name(eid),
                "attributes": {lab.get(a, a): row.get(a) for a in keep if a in row}}


# ------------------------------------------------------------------ 值过滤（属性取值出现在问题里）
def value_filters(q: str, schema: dict, F: dict, cls: str) -> list[dict]:
    """问题里出现的属性取值 → 过滤条件。

    同一个取值可能同时出现在多列里——典型是层级组织宽表：「数据管理部」既是某些人的
    末级部门（dept_cn_name），又是全体成员的 5 级部门（dept_cn_name_lv5）。
    按业务语义，「数据管理部的人」= 该部门及其所有下级部门的人，所以同值多列取 **并集（OR）**，
    而不是挑一列（挑末级部门列只会命中直属的 1 个人）。
    返回 [{"value", "attrs": [列...], "attr": 覆盖最多的列}]，过滤时按 attrs 取并集。
    """
    df = F.get(cls)
    if df is None:
        return []
    by_val: dict[str, dict] = {}
    for a, m in schema["attributes"].items():
        if m.get("owner") != cls or a not in df or m.get("role") not in ("enum", "name", "text"):
            continue
        col = df[a].dropna().astype(str)
        if col.nunique() > 500:
            continue
        for v in col.unique():
            nv = _norm(v)
            if (len(nv) >= 2 and nv in q) or (len(nv) == 1 and re.search(re.escape(nv) + r"(性|生|士|的人|员工)", q)):
                hit = by_val.setdefault(nv, {"value": v, "attrs": [], "cover": {}})
                hit["attrs"].append(a)
                hit["cover"][a] = int((col == v).sum())
    # 长值优先（「数据管理部」优先于「数据」），被更长取值包含的短值丢弃
    vals = sorted(by_val, key=len, reverse=True)
    keep = [v for i, v in enumerate(vals) if not any(v in w for w in vals[:i])]
    out = []
    for nv in keep[:3]:
        h = by_val[nv]
        out.append({"value": h["value"], "attrs": h["attrs"],
                    "attr": max(h["attrs"], key=lambda a: h["cover"][a])})
    return out


def apply_filters(df, filters: list[dict]):
    """每个条件内多列 OR，条件之间 AND。比较时忽略括注（「刘涛 (Liu Tao)」=「刘涛」）。"""
    import pandas as pd
    mask = pd.Series(True, index=df.index)
    for f in filters:
        nv = _norm(f["value"])
        m = pd.Series(False, index=df.index)
        for a in f.get("attrs") or [f["attr"]]:
            m |= df[a].astype(str).map(_norm) == nv
        mask &= m
    return df[mask]


# ------------------------------------------------------------------ 确定性检索
def retrieve(q: str, schema: dict, F: dict, L: dict, idx: Index | None = None) -> dict | None:
    idx = idx or Index(schema, F, L)
    classes = list(schema["entity_types"])
    lab = {a: (schema["attributes"][a].get("label") or a) for a in schema["attributes"]}

    # 1) 实体 + 属性 / 关系 / 档案
    ents = idx.link(q)
    attrs = match_attrs(q, schema)
    if ents and not _GROUP.search(q):
        cls0 = idx.ent[ents[0]]["cls"]
        own = [a for a in attrs if schema["attributes"][a]["owner"] == cls0]
        foreign = [a for a in attrs if a not in own]
        # 「研发部的平均薪资」：实体只是过滤条件，问的是别的类的度量 → 交给指标计划
        if _AGG.search(q) and foreign and not own:
            return None
        qr = idx.strip(q)
        rel_hits0 = [n for n in idx.neighbors(ents[0]) if _rel_hit(qr, schema, n, idx)]
        other = [c for c in classes if c != cls0 and any(w in qr for w in _class_words(schema, c))]
        # 「研发部有多少人 / 有哪些人」：没有点名具体关系时，沿全部关系聚合该类邻居
        if (_COUNT.search(qr) or _LIST.search(qr)) and other and not _specific_rel(qr, schema, rel_hits0):
            tgt_cls = other[0]
            res = []
            for eid in ents[:3]:
                mem = {n["id"]: n for n in idx.neighbors(eid) if idx.ent.get(n["id"], {}).get("cls") == tgt_cls}
                res.append({"entity": idx.name(eid), "relation": sorted({n["relation"] for n in mem.values()}),
                            "count": len(mem), "members": [n["entity"] for n in mem.values()][:50]})
            return {"op": "neighbor_count" if _COUNT.search(qr) else "neighbor_list",
                    "class": schema["entity_types"][tgt_cls].get("label") or tgt_cls, "items": res}
        items = []
        for eid in ents[:5]:
            cls = idx.ent[eid]["cls"]
            own_e = [a for a in attrs if schema["attributes"][a]["owner"] == cls]
            rel_hits = [n for n in idx.neighbors(eid) if _rel_hit(qr, schema, n, idx)]
            if _specific_rel(qr, schema, rel_hits):  # 点名了具体关系：只保留这些关系
                spec = {n["relation"] for n in rel_hits if _rel_word_hit(qr, schema, n)}
                rel_hits = [n for n in rel_hits if n["relation"] in spec]
                # 方向：「下属/带了」看入边，「上级/汇报给谁」看出边
                if re.search(r"(下属|下级|团队|带了|带几)", qr):
                    rel_hits = [n for n in rel_hits if n["direction"] == "in"] or rel_hits
                elif re.search(r"(上级|汇报给谁|主管是|领导是)", qr):
                    rel_hits = [n for n in rel_hits if n["direction"] == "out"] or rel_hits
            p = idx.profile(eid, own_e or None)
            p["matched_relations"] = rel_hits
            p["neighbors"] = idx.neighbors(eid)[:15]
            if rel_hits and not own_e:
                p["attributes"] = {}                  # 问的是关系，不堆属性
            items.append(p)
        return {"op": "lookup", "entities": items, "count_question": bool(_COUNT.search(qr) or re.search(r"几个", qr)),
                "asked_attributes": [lab[a] for a in attrs if any(
                    schema["attributes"][a]["owner"] == idx.ent[x]["cls"] for x in ents[:5])]}

    # 2) 分组 / 过滤（默认对象：问题里提到的类，或只有一个类时取该类）
    # 被计数的类：「人数/多少人」→ 人员类；否则取问题里提到的类
    counted = next((c for c in classes if re.search(r"(人数|多少人|几个人|几人|人员|员工)", q)
                    and c in ("Person",) or (re.search(r"(人数|多少人)", q)
                                             and any(w in ("人员", "员工") for w in _class_words(schema, c)))), None)
    cls = counted or next((c for c in classes if any(w in q for w in _class_words(schema, c))), None)
    if cls is None and len(classes) == 1:
        cls = classes[0]
    if cls is None:
        cls = max(classes, key=lambda c: len(F.get(c, [])), default=None)
    if cls is None:
        return None
    df = F[cls]
    # 「X 的平均/合计/最高 + 度量」是指标问题：交给指标计划（含过滤），不在这里按人数作答
    meas_hit = [a for a in match_attrs(q, schema, cls) if schema["attributes"][a].get("role") == "measure"]
    # 属性名里自带的「最高/平均」（最高学历、平均分档）不算聚合词
    q_agg = q
    for a in match_attrs(q, schema, cls):
        for w in [schema["attributes"][a].get("label") or "", *attr_words(schema, a)]:
            if len(w) >= 2:
                q_agg = q_agg.replace(w, " ")
    if _AGG_STRICT.search(q_agg) and meas_hit:
        return None
    vf = value_filters(q, schema, F, cls)
    sub = apply_filters(df, vf)
    cls_label = schema["entity_types"][cls].get("label") or cls
    scope = "，".join(_norm(f["value"]) for f in vf)

    # 聚合类问题（平均/合计/最高…）交给指标；分组计数只做「人数/数量/分布/比例」
    if _GROUP.search(q) and not _AGG_STRICT.search(q_agg):
        cand = [a for a in match_attrs(q, schema, cls)
                if a in df and 1 < df[a].nunique() <= GROUP_MAX_DISTINCT
                and schema["attributes"][a].get("role") in ("enum", "name", "text")]
        # 分组属性必须属于「被计数的那个类」：问「各部门人数」，在只有部门类自身名称时不能对部门计数
        names = {schema["entity_types"][c].get("name_column") for c in classes}
        cand = [a for a in cand if (schema["attributes"][a].get("column") or a) not in names
                and not any(a in f["attrs"] for f in vf)]
        # 人名列（汇报人/负责人姓名）不作为分组口径，除非问题直接点名该属性
        pn = set(df["_name"].astype(str).map(_norm))
        cand = [a for a in cand if not (set(df[a].dropna().astype(str).map(_norm)) & pn)
                or (schema["attributes"][a].get("label") or "") in q] or cand
        if cand:
            a = cand[0]
            vc = sub[a].fillna("空值").astype(str).map(_norm).replace("", "空值").value_counts()
            total = int(vc.sum())
            return {"op": "group", "class": cls_label, "attribute": lab[a], "scope": scope,
                    "total": total, "filters": [{"value": f["value"], "columns": f["attrs"]} for f in vf],
                    "groups": [{"value": k, "count": int(v), "share": round(v / total * 100, 1)}
                               for k, v in vc.items()]}
    if vf and (_COUNT.search(q) or _LIST.search(q)):
        rows = [idx.profile(r["_id"]) for r in sub.head(MAX_CONTEXT_ROWS).to_dict(orient="records")]
        return {"op": "filter_list" if _LIST.search(q) and not _COUNT.search(q) else "filter_count",
                "class": cls_label, "scope": scope,
                "filters": [{"attribute": "/".join(dict.fromkeys(lab[a] for a in f["attrs"])),
                             "columns": f["attrs"], "value": f["value"]} for f in vf],
                "count": int(len(sub)), "members": [r["name"] for r in rows][:50]}
    ym = re.search(r"(\d{4})\s*年?\s*(以后|之后|后|以来|起|以前|之前|前)", q)
    if ym and _COUNT.search(q):
        import pandas as pd
        # 时间属性决定对象类：「2025年后开始的有多少个」→ 拥有 start_date 的项目类
        tall = [(a, m["owner"]) for a, m in schema["attributes"].items()
                if (m.get("role") == "temporal" or m.get("range") == "date") and m["owner"] in F
                and any(w in q for w in attr_words(schema, a) + (["出生"] if "birth" in a else []))]
        if tall and tall[0][1] != cls and not vf:
            cls = tall[0][1]
            df = sub = F[cls]
            cls_label = schema["entity_types"][cls].get("label") or cls
        tcols = [a for a, m in schema["attributes"].items() if m["owner"] == cls and a in df
                 and (m.get("role") == "temporal" or m.get("range") == "date")]
        hit = [a for a in tcols if any(w in q for w in attr_words(schema, a) + ["出生" if "birth" in a else "~"])]
        if hit:
            a = hit[0]
            ts = pd.to_datetime(sub[a], errors="coerce")
            y = pd.Timestamp(f"{ym.group(1)}-01-01")
            after = ym.group(2) in ("以后", "之后", "后", "以来", "起")
            n = int((ts >= y).sum() if after else (ts < y).sum())
            return {"op": "filter_count", "class": cls_label,
                    "scope": "，".join(x for x in (scope, f"{lab[a]}{ym.group(1)}年{'起' if after else '以前'}") if x),
                    "filters": [{"attribute": lab[a], "value": f"{'>=' if after else '<'}{ym.group(1)}"}],
                    "count": n, "members": []}
    if not vf and _COUNT.search(q) and re.search(r"(一共|总共|总|全部|所有|共有)", q):
        return {"op": "filter_count", "class": cls_label, "scope": "", "filters": [], "count": int(len(df)),
                "members": []}
    if _LIST.search(q) and not vf:
        return {"op": "filter_list", "class": cls_label, "scope": "", "filters": [],
                "count": int(len(df)), "members": [_norm(n) for n in df["_name"].astype(str).head(50)]}
    return None


_CLASS_ZH = {"Person": ["人员", "员工", "人", "同事", "成员"], "OrganizationUnit": ["部门", "组织", "单位"],
             "Project": ["项目"], "Document": ["文档", "文件"], "System": ["系统"], "Skill": ["技能"]}


def _class_words(schema: dict, cls: str) -> list[str]:
    v = schema["entity_types"].get(cls) or {}
    ws = [v.get("label"), cls, *(v.get("aliases") or []), *_CLASS_ZH.get(cls, [])]
    return [w for w in dict.fromkeys(ws) if w and (len(str(w)) >= 2 or w == "人")]


def _rel_word_hit(q: str, schema: dict, n: dict) -> bool:
    """关系语义词命中（owner→负责人）；关系标签只在不与类名重叠时才算（「人员-项目关联」≠「项目」）。"""
    if any(w and w in q for w in _rel_words(schema, n["relation"])):
        return True
    return len(n["relation"]) >= 3 and n["relation"] in q


def _specific_rel(q: str, schema: dict, hits: list[dict]) -> bool:
    return any(_rel_word_hit(q, schema, n) for n in hits)


def _rel_hit(q: str, schema: dict, n: dict, idx) -> bool:
    """问题是否在问这条关系：关系标签 / 关系列语义词（owner→负责人）/ 另一端类的中文名。"""
    if _rel_word_hit(q, schema, n):
        return True
    other = idx.ent.get(n["id"], {}).get("cls")
    return bool(other) and any(len(w) >= 2 and w in q for w in _class_words(schema, other))


def _rel_words(schema: dict, rel_label: str) -> list[str]:
    for k, v in schema["relation_types"].items():
        if (v.get("label") or k) == rel_label:
            ws = []
            for col in {(v.get("column") or "").lower(), k.lower()} - {""}:
                ws += [w for pat, wl in ATTR_WORDS if re.search(pat, col) for w in wl]
            return list(dict.fromkeys(ws))
    return []


# ------------------------------------------------------------------ 本体上下文（给 LLM 的依据）
def schema_brief(schema: dict, F: dict, max_attrs: int = 40) -> dict:
    out = {"classes": {}, "relations": {}}
    for c, v in schema["entity_types"].items():
        df = F.get(c)
        attrs = [a for a, m in schema["attributes"].items() if m["owner"] == c]
        attrs.sort(key=lambda a: -_attr_rank(schema, a))
        out["classes"][c] = {
            "label": v.get("label") or c, "instances": int(len(df)) if df is not None else 0,
            "attributes": {(schema["attributes"][a].get("label") or a): {
                "column": a, "role": schema["attributes"][a].get("role"),
                "examples": [_norm(x) for x in (df[a].dropna().astype(str).unique()[:4]
                                                 if df is not None and a in df else [])]}
                for a in attrs[:max_attrs]}}
    for r, v in schema["relation_types"].items():
        out["relations"][v.get("label") or r] = {"from": v["source"], "to": v["target"],
                                                 "cardinality": v.get("cardinality")}
    return out


def keyword_rows(q: str, idx: Index, limit: int = 20) -> list[dict]:
    """兜底检索：按问题中的中文片段在实例属性值里做包含匹配（不送全量数据）。"""
    grams = {q[i:i + n] for n in (4, 3, 2) for i in range(len(q) - n + 1)
             if re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]+", q[i:i + n])}
    stop = {"多少", "哪些", "什么", "是否", "有没", "一下", "请问", "这个", "那个", "人员", "员工"}
    grams -= stop
    scored = []
    for eid, e in idx.ent.items():
        text = " ".join(_norm(v) for v in e["row"].values() if isinstance(v, str))
        s = sum(len(g) for g in grams if g in text)
        if s:
            scored.append((s, eid))
    scored.sort(reverse=True)
    return [idx.profile(eid) for _, eid in scored[:limit]]


_SYSTEM = """你是企业本体问答助手。只能依据「依据」中的本体结构、实体、关系和计算结果回答。
不得补造实体、属性、关系、数字或业务常识；依据不足、冲突或无法唯一定位时，明确说明“现有本体数据无法确认”，并指出缺少的证据。
回答必须简洁：优先 1 句结论，必要时最多补充 3 条关键依据；不输出推理过程、不重复问题、不扩展背景。
数字必须与计算结果完全一致；涉及实体时优先使用依据中的实体名称和 ID；用中文。"""


def grounded_answer(q: str, evidence: dict, llm) -> str | None:
    try:
        return (llm.chat(_SYSTEM, json.dumps({"问题": q, "依据": evidence},
                                              ensure_ascii=False, default=str)[:24000],
                         temperature=0.1, timeout=60) or "").strip() or None
    except Exception:  # noqa: BLE001  LLM 不可用 → 模板回答
        return None


# ------------------------------------------------------------------ 模板回答
def template_answer(r: dict) -> str:
    op = r["op"]
    if op in ("neighbor_count", "neighbor_list"):
        return "\n".join(f"{x['entity']}{('（' + '/'.join(x['relation']) + '）') if x['relation'] else ''}"
                         f"共有 {x['count']} 个{r['class']}" +
                         (f"：{'、'.join(x['members'][:30])}" if op == "neighbor_list" or x['count'] <= 30 else "")
                         for x in r["items"])
    if op == "lookup":
        parts = []
        for e in r["entities"]:
            if e.get("matched_relations") and r.get("count_question"):
                parts.append(f"{e['name']}共 {len(e['matched_relations'])} 个：" +
                             "、".join(n["entity"] for n in e["matched_relations"][:30]))
            elif e.get("matched_relations"):
                ms = e["matched_relations"]
                rel = "；".join(f"{n['relation']}{'→' if n['direction'] == 'out' else '←'}{n['entity']}"
                               for n in ms[:50]) + (f"（共 {len(ms)} 个）" if len(ms) > 3 else "")
                kv = "；".join(f"{k}：{v}" for k, v in e["attributes"].items() if v is not None)
                parts.append(f"{e['name']}：{rel}" + (f"；{kv}" if kv else ""))
            elif r["asked_attributes"] and e["attributes"]:
                kv = "；".join(f"{k}：{v}" for k, v in e["attributes"].items() if v is not None)
                parts.append(f"{e['name']}的{kv}")
            else:
                kv = "；".join(f"{k}：{v}" for k, v in list(e["attributes"].items())[:12] if v is not None)
                nb = "；".join(f"{n['relation']}{'→' if n['direction'] == 'out' else '←'}{n['entity']}"
                              for n in e["neighbors"][:8])
                parts.append(f"{e['name']}（{e['class']}）：{kv}" + (f"。关系：{nb}" if nb else ""))
        return "\n".join(parts) or "找到了实体，但没有对应属性值"
    if op == "group":
        g = "；".join(f"{x['value']} {x['count']}（{x['share']}%）" for x in r["groups"][:15])
        head = f"{r['scope']}的" if r["scope"] else ""
        return f"{head}{r['class']}按{r['attribute']}分布（共 {r['total']}）：{g}"
    if op == "filter_count" and not r["scope"]:
        return f"{r['class']}共 {r['count']} 个"
    if op == "filter_count":
        return f"{r['scope']}的{r['class']}共 {r['count']} 个" + (
            f"：{'、'.join(r['members'][:20])}" if r["members"] else "")
    if op == "filter_list":
        head = f"{r['scope']}的" if r["scope"] else ""
        return f"{head}{r['class']}共 {r['count']} 个：{'、'.join(r['members'][:30])}" + \
            ("…" if r["count"] > 30 else "")
    return ""


def chart_of(r: dict) -> dict | None:
    if r and r.get("op") == "group":
        return {"type": "bar", "title": f"{r['class']}·{r['attribute']}分布",
                "categories": [g["value"] for g in r["groups"][:30]],
                "data": [g["count"] for g in r["groups"][:30]]}
    return None
