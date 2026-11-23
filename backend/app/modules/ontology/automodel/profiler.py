"""① 数据画像：列语义类型、基数、唯一性、候选主键、隐式外键。纯函数。"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

import pandas as pd

from .connectors import ForeignKey, TableSet

ID_NAME = re.compile(r"(^id$|_id$|_no$|_code$|_key$|^code$|编号$|代码$|工号$)", re.I)
DATE_NAME = re.compile(r"(date|time|_dt$|_at$|日期|时间)", re.I)
ENUM_MAX_DISTINCT = 50
ENUM_MAX_RATIO = 0.05
FK_MIN_CONTAIN = 0.95
# 自引用外键常见前缀：manager_emp_no → employees，parent_dept_id → departments
SELF_REF = {"manager", "leader", "supervisor", "parent", "superior", "reports", "上级"}


@dataclass
class ColumnProfile:
    name: str
    dtype: str                 # integer / number / text / date / boolean
    role: str                  # identifier / foreign_key / enum / measure / temporal / text / name
    null_ratio: float
    distinct: int
    distinct_ratio: float
    unique: bool
    samples: list = field(default_factory=list)
    comment: str = ""
    label: str = ""


@dataclass
class TableProfile:
    name: str
    rows: int
    primary_key: list[str]
    pk_inferred: bool
    columns: dict[str, ColumnProfile]
    foreign_keys: list[dict]
    is_junction: bool = False
    comment: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["columns"] = {k: asdict(v) for k, v in self.columns.items()}
        return d


def keyset(s: pd.Series) -> set[str]:
    """键值归一：浮点化的整数（含空值列）1001.0 → "1001"，避免外键包含率误判。"""
    out = set()
    for v in s.dropna():
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        out.add(str(v).strip())
    return out


def _dtype(s: pd.Series) -> str:
    nn = s.dropna()
    if nn.empty:
        return "text"
    if pd.api.types.is_bool_dtype(nn):
        return "boolean"
    if pd.api.types.is_integer_dtype(nn):
        return "integer"
    if pd.api.types.is_float_dtype(nn):
        return "integer" if (nn % 1 == 0).all() else "number"
    if pd.api.types.is_datetime64_any_dtype(nn):
        return "date"
    sample = nn.astype(str).head(200)
    if sample.str.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T].*)?").mean() > 0.9:
        return "date"
    if sample.str.fullmatch(r"-?\d+").mean() > 0.95:
        return "integer"
    if sample.str.fullmatch(r"-?\d+(\.\d+)?").mean() > 0.95:
        return "number"
    return "text"


def profile_column(name: str, s: pd.Series, comment: str = "", label: str = "", role_override: str | None = None) -> ColumnProfile:
    n = len(s)
    nn = s.dropna()
    distinct = int(nn.nunique())
    ratio = distinct / len(nn) if len(nn) else 0.0
    dt = _dtype(s)
    unique = len(nn) > 0 and distinct == len(nn) and s.isna().sum() == 0
    if ID_NAME.search(name) and (unique or dt in ("integer", "text")):
        role = "identifier"
    elif dt == "date" or DATE_NAME.search(name):
        role = "temporal"
    elif dt == "text" and re.search(r"(name|title|名称|姓名|标题)", name, re.I):
        role = "name"
    elif dt in ("integer", "number") and re.search(r"(^age$|_age$|年龄)", name, re.I):
        role = "measure"
    elif dt in ("integer", "number") and re.search(
            r"(birth|city|state|country|province|region|zip|postal|area|district|location|_rcd$|seq$|"
            r"_cd$|_no$|_num$|phone|mobile|tel|code|编码|代码|行政区)", name, re.I):
        role = "enum" if distinct <= ENUM_MAX_DISTINCT else "identifier"   # 行政区划/编码：数字但不可加总
    elif dt == "number" or (dt == "integer" and distinct > ENUM_MAX_DISTINCT // 5) \
            or (dt in ("integer", "number") and re.search(
                r"(amount|amt|price|cost|budget|salary|qty|count|score|金额|预算|薪|数量)", name, re.I)):
        role = "measure"
    elif distinct <= ENUM_MAX_DISTINCT and ratio <= max(ENUM_MAX_RATIO, 10 / max(len(nn), 1)):
        role = "enum"
    elif dt in ("integer", "number"):
        role = "measure"
    else:
        role = "text"
    if role_override and role_override in {"identifier", "name", "enum", "measure", "temporal", "text", "boolean"}:
        role = role_override
    return ColumnProfile(name=name, dtype=dt, role=role,
                         null_ratio=round(float(s.isna().mean()) if n else 0, 3),
                         distinct=distinct, distinct_ratio=round(ratio, 3), unique=bool(unique),
                         samples=[str(x) for x in nn.unique()[:5]], comment=comment, label=label)


def _candidate_pk(tname: str, cols: dict[str, ColumnProfile]) -> list[str]:
    # 日期/度量列即使恰好唯一也不作主键（出生日期、金额会重复，只是样本小）
    uniq = [c for c in cols.values() if c.unique and c.role not in ("temporal", "measure") and c.dtype != "date"]
    if not uniq:
        return []
    def rank(c):  # 名字像 id 的优先；与表名同前缀的优先；名称类列最后（姓名可能重名）
        return (0 if c.name in ("id", f"{tname}_id") else 1 if ID_NAME.search(c.name) or re.search(r"(id$|编号|工号)", c.name, re.I) else
                3 if re.search(r"(name|名称|姓名)", c.name, re.I) else 2,
                0 if c.dtype == "integer" else 1)
    return [sorted(uniq, key=rank)[0].name]


def infer_foreign_keys(ts: TableSet, profiles: dict[str, TableProfile]) -> None:
    """取值包含率 ≥95% 且列名可对上 → 隐式外键（declared=False）。"""
    # 同名主键归属：多张表推断出同名单列主键（如 employees.emp_no 与 project_members.emp_no）时，
    # 只有「表名与键名相关、或取值最多」的那张是真正的主表，其余视为外键持有方
    by_pk: dict[str, list[str]] = {}
    for t, p in profiles.items():
        if len(p.primary_key) == 1:
            by_pk.setdefault(p.primary_key[0], []).append(t)
    suspect: set[str] = set()
    for key, tabs in by_pk.items():
        if len(tabs) < 2:
            continue
        stem = re.sub(ID_NAME, "", key).rstrip("_")

        def own(tb):
            return (bool(stem) and stem in tb, len(keyset(ts.tables[tb].df[key])))
        owner = max(tabs, key=own)
        suspect |= {tb for tb in tabs if tb != owner and profiles[tb].pk_inferred}
    pk_values = {}
    for t, p in profiles.items():
        if len(p.primary_key) == 1 and t not in suspect:
            pk_values[t] = keyset(ts.tables[t].df[p.primary_key[0]])
    for t, p in profiles.items():
        declared = {fk["column"] for fk in p.foreign_keys}
        for c in p.columns.values():
            # 推断主键被判为「同名主键的非主表」→ 其实是外键（小样本时外键列也可能恰好唯一）
            pk_suspect = t in suspect and c.name in p.primary_key
            if c.name in declared or (c.name in p.primary_key and not pk_suspect) \
                    or c.dtype not in ("integer", "text"):
                continue
            if not ID_NAME.search(c.name):
                continue
            vals = keyset(ts.tables[t].df[c.name])
            if not vals:
                continue
            best = None
            for rt, pv in pk_values.items():
                if not pv:
                    continue
                contain = len(vals & pv) / len(vals)
                stem = re.sub(ID_NAME, "", c.name).rstrip("_")
                name_hit = bool(stem) and (stem in rt or rt.rstrip("s") in stem
                                           or (rt == t and stem.split("_")[0] in SELF_REF))
                score = contain + (0.1 if name_hit else 0) - (0.2 if rt == t and not name_hit else 0)
                if contain >= FK_MIN_CONTAIN and (best is None or score > best[0]):
                    best = (score, rt, contain, name_hit)
            if best:
                _, rt, contain, name_hit = best
                p.foreign_keys.append({"column": c.name, "ref_table": rt,
                                       "ref_column": profiles[rt].primary_key[0],
                                       "declared": False, "containment": round(contain, 3),
                                       "name_match": name_hit})
                c.role = "foreign_key"


def profile(ts: TableSet) -> dict[str, TableProfile]:
    out: dict[str, TableProfile] = {}
    for t, tbl in ts.tables.items():
        cols = {c: profile_column(c, tbl.df[c], tbl.column_comments.get(c, ""),
                                   tbl.column_labels.get(c, ""), tbl.column_roles.get(c))
                for c in tbl.df.columns}
        pk = list(tbl.primary_key) or _candidate_pk(t, cols)
        for key in pk:
            if key in cols:
                cols[key].role = "identifier"
        for c in pk:
            cols[c].role = "identifier"
        fks = [{"column": fk.column, "ref_table": fk.ref_table, "ref_column": fk.ref_column,
                "declared": True, "containment": 1.0, "name_match": True}
               for fk in tbl.foreign_keys]
        for fk in fks:
            if fk["column"] in cols:
                cols[fk["column"]].role = "foreign_key"
        out[t] = TableProfile(name=t, rows=tbl.row_count or len(tbl.df), primary_key=pk,
                              pk_inferred=not tbl.primary_key, columns=cols,
                              foreign_keys=fks, comment=tbl.comment)
    infer_foreign_keys(ts, out)
    for p in out.values():
        fk_cols = {fk["column"] for fk in p.foreign_keys}
        if p.pk_inferred and set(p.primary_key) & fk_cols:
            # 主键列被识别为外键：改用「全部外键列」作复合键（关联表）或清空待人工指定
            p.primary_key = sorted(fk_cols) if len(fk_cols) >= 2 else []
        business = [c for c in p.columns if c not in fk_cols and c not in p.primary_key]
        p.is_junction = len(fk_cols) == 2 and len(business) <= 1
    return out
