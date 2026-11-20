"""映射建议（auto-draft）：把「人写契约」降级为「人审建议」。

第一性原理：契约的全部内容是「外部列 → 本体谓词」的绑定。其中绝大部分不是判断，
而是**能从数据本身算出来的事实**：

    pk_column        → 非空率 100% 且采样内无重复的列
    name_column      → 与 pk 一对一、值形态为文本短串的列
    entity_type      → 由主键列名/来源系统决定（工号 → Person）
    expect_columns   → 就是本文件的列集合
    required         → 非空率 100%
    object_kind      → 列值在既有实体集合里命中率高则是 entity_ref，否则 literal
    valid_from/to    → 列名含生效/开始 + 值可解析为日期的列
    restricted       → 列名命中敏感词（绩效 / 薪资 / 身份证…）默认不落库

剩下只有一格需要判断：**这个列在语义上叫什么谓词**。列名词典能覆盖常见组织/HR
列名，未命中的才交给模型给候选——模型只产出「建议值」，被采纳后它就是 mapping
里的一个确定字符串，写入仍由零 LLM 的 `assertions.build_assertions` 执行。
模型不可用时整条链路照常工作：留空、默认不入库、由人补一行。

因此本模块**没有任何写库动作**，纯函数 + 可选的一次模型调用。
"""
from __future__ import annotations

import re

# 采样规模：宽表降低采样，避免 512 列 × 1000 行的去重集合吃掉进程内存。
_SAMPLE_WIDE = (64, 256)
_LLM_MAX_COLUMNS = 40          # 一次请求最多问多少列（超出部分留空，由人补）
_LLM_CONFIDENCE = 0.6

# 敏感列：默认 restricted（不落库），与评估文档「绩效默认不落库」一致。
# 中文按子串匹配；英文按「完整词段」匹配——否则 manager_id 会因为含 age 被误判。
_SENSITIVE_HINTS = (
    "salary", "wage", "payroll", "compensation", "bonus", "薪资", "工资", "薪酬", "奖金",
    "performance", "rating", "kpi", "绩效", "考核", "评分",
    "idcard", "idnumber", "身份证", "银行卡", "bankcard",
    "birth", "birthday", "生日", "出生", "age", "年龄",
    "phone", "mobile", "telephone", "手机", "电话", "住址", "address",
)

# 列名词典：normalize 后做子串匹配。顺序即优先级，先命中先得。
# (关键词, 谓词, 对象类型, 目标实体类型, 关系名, 是否单值)
_COLUMN_DICT: tuple[tuple[tuple[str, ...], str, str, str, str, bool], ...] = (
    (("manager", "superior", "supervisor", "leader", "主管", "上级", "汇报对象", "汇报给"),
     "reports_to", "entity_ref", "Person", "reports_to", True),
    (("department", "dept", "orgunit", "org_unit", "orgname", "部门", "所属部门", "组织"),
     "department", "literal", "", "", True),
    (("jobtitle", "job_title", "position", "positionname", "职位", "岗位", "职务"),
     "job_title", "literal", "", "", True),
    (("joblevel", "job_level", "grade", "level", "rank", "职级", "级别", "职等"),
     "level", "literal", "", "", True),
    (("employmentstatus", "empstatus", "status", "在职", "状态"),
     "status", "literal", "", "", True),
    (("email", "mail", "邮箱", "邮件"),
     "email", "literal", "", "", False),
    (("hiredate", "hire_date", "joindate", "join_date", "entrydate", "入职", "入职时间", "入职日期"),
     "hired_at", "literal", "", "", True),
    (("location", "office", "workplace", "city", "地点", "办公地", "城市", "工作地"),
     "location", "literal", "", "", True),
    (("company", "employer", "公司", "所属公司", "法人"),
     "works_at", "literal", "", "", True),
    (("employeeid", "empid", "staffid", "employeeno", "staffno", "工号", "员工编号", "员工id"),
     "", "literal", "", "", False),   # 主键候选，不生成谓词
    (("employeename", "empname", "staffname", "fullname", "姓名", "名字"),
     "", "literal", "", "", False),   # 名称列候选，不生成谓词
)

_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}([-/]\d{1,2})?$|^\d{8}$|^\d{4}年\d{1,2}月"
                      r"|^\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{2}:\d{2}")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
_NON_WORD = re.compile(r"[\s_\-./·()（）]+")


def _norm(name: str) -> str:
    return _NON_WORD.sub("", str(name or "").lower())


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _sample_size(ncols: int, total: int) -> int:
    if ncols <= _SAMPLE_WIDE[0]:
        return min(total, 1000)
    if ncols <= _SAMPLE_WIDE[1]:
        return min(total, 400)
    return min(total, 150)


def profile_columns(rows: list[dict]) -> dict[str, dict]:
    """逐列统计：非空率、采样内去重数、日期/数字占比、少量样例值。

    只保留「去重集合 + 最多 8 个样例」，内存与行数无关、与列数线性相关。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return {}
    columns: list[str] = []
    seen: set[str] = set()
    for r in rows[:50]:
        for k in r.keys():
            if str(k) not in seen:
                seen.add(str(k))
                columns.append(str(k))
    n = _sample_size(len(columns), len(rows))
    sample = rows[:n]

    prof: dict[str, dict] = {
        c: {"nonnull": 0, "values": set(), "overflow": False, "dup": False,
            "sum_len": 0, "date_hits": 0, "num_hits": 0, "samples": []}
        for c in columns
    }
    for row in sample:
        for c in columns:
            raw = row.get(c)
            t = _text(raw)
            if not t:
                continue
            p = prof[c]
            p["nonnull"] += 1
            p["sum_len"] += len(t)
            if t in p["values"]:
                p["dup"] = True
            elif len(p["values"]) < n:
                p["values"].add(t)
            else:
                p["overflow"] = True
            if _DATE_RE.match(t):
                p["date_hits"] += 1
            if _NUM_RE.match(t):
                p["num_hits"] += 1
            if len(p["samples"]) < 8 and t not in p["samples"]:
                p["samples"].append(t)

    out: dict[str, dict] = {}
    for c, p in prof.items():
        nn = p["nonnull"]
        distinct = len(p["values"])
        out[c] = {
            "column": c,
            "nonnull_rate": round(nn / n, 4) if n else 0.0,
            "distinct": distinct,
            "unique": bool(nn == n and not p["dup"] and not p["overflow"]),
            "avg_len": round(p["sum_len"] / nn, 1) if nn else 0.0,
            "date_rate": round(p["date_hits"] / nn, 3) if nn else 0.0,
            "num_rate": round(p["num_hits"] / nn, 3) if nn else 0.0,
            "samples": p["samples"][:5],
        }
    return out


def _pick_pk(prof: dict[str, dict], columns: list[str]) -> tuple[str, str]:
    """主键：优先「唯一 + 列名像业务主键」，其次「唯一 + 高基数文本」。

    候选按列名排序后再挑：上传快照存 JSONB，键序不保证与文件一致，
    若依赖原始顺序，同一份文件两次推导可能得到不同主键。

    词表分两级：「emplid / 工号」这类实体标识词强于泛化的 id/code；
    事件类列名片段（action/event/操作…）永远不参与主键——它们描述
    「发生了什么」，不是「这条数据是谁」。
    """
    candidates = sorted(c for c in columns if prof[c]["unique"])
    if not candidates:
        best = max(columns, key=lambda c: (prof[c]["nonnull_rate"],
                                          prof[c]["distinct"], c))
        return best, "无唯一列，退回非空率最高的列，需你确认"
    normed = {c: _norm(c) for c in candidates}

    def ok(c: str) -> bool:
        return not any(x in normed[c] for x in _PK_EVENT_EXCLUDE)

    # 1) 实体标识强词：emplid / employee_id / 工号 …
    for c in candidates:
        if ok(c) and any(w in normed[c] for w in _PK_STRONG):
            return c, "非空率 100% 且采样内无重复，列名是实体标识词"
    # 2) 列名以 id / no / num 结尾（比泛含更严，避开 action_code 这类）
    for c in candidates:
        if ok(c) and (normed[c].endswith("id") or normed[c].endswith("no")
                      or normed[c].endswith("num")):
            return c, "非空率 100% 且采样内无重复，列名以主键特征词结尾"
    # 3) code / 编码 结尾
    for c in candidates:
        if ok(c) and (normed[c].endswith("code") or normed[c].endswith("编码")):
            return c, "非空率 100% 且采样内无重复，列名以编码特征词结尾"
    # 4) 泛特征词兜底（不排除事件列——此时已无更好选择，但理由要写明）
    id_words = ("id", "code", "no", "num", "工号", "编号", "编码")
    for c in candidates:
        if any(w in normed[c] for w in id_words):
            return c, "非空率 100% 且采样内无重复，列名含主键特征词，需你确认"
    for c in candidates:
        if prof[c]["num_rate"] > 0.8 or prof[c]["avg_len"] <= 20:
            return c, "非空率 100% 且采样内无重复"
    return candidates[0], "非空率 100% 且采样内无重复"


# 实体标识强词：比泛化的 id/code 更可信（归一后匹配）
_PK_STRONG = ("emplid", "employeeid", "empid", "staffid", "personid", "userid",
              "memberid", "workerid", "工号", "员工编号", "员工id")
# 事件/流程类列名片段：是「动作」不是「实体」，绝不能当主键
_PK_EVENT_EXCLUDE = ("action", "event", "transaction", "operation", "change",
                     "request", "workflow", "task", "动作", "事件", "操作", "流程")


_NAME_STRONG = ("employeename", "staffname", "fullname", "realname", "personname",
                "displayname", "姓名", "名字", "empname", "membername")
# 部门 / 组织 / 项目类列名里常带 name，绝不能被当成人名
_NAME_EXCLUDE = ("dept", "department", "org", "company", "team", "project",
                 "city", "account", "group", "file", "table", "role", "job",
                 "action", "event", "sex", "gender", "post", "status", "region",
                 "political", "officer", "worktype", "linerep", "report", "boss",
                 "manager", "动作", "性别", "岗位", "状态", "主管", "汇报",
                 "部门", "组织", "公司", "项目", "团队", "城市")
# 属性展示名模式：xxx_cn_name / xxx_en_name 是「某属性的中/英文叫法」，
# HR 导出里几十列都是这种，绝大多数不是人名（action_cn_name=调动）。
_NAME_CN_EN_SUFFIX = ("cnname", "enname")
# 人名值启发：2~4 个纯汉字（某某 / 欧阳娜娜），不含空格标点
_NAME_CN_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")


def _pick_name(prof: dict[str, dict], columns: list[str], pk: str) -> tuple[str, str]:
    """名称列：精确 name → 强特征词 → 属性名排除后的 name → 人名值启发
    → 属性展示名 → 与主键一一对应的文本短列。"""
    cands = sorted(c for c in columns if c != pk)

    def attr_display(c: str) -> bool:
        k = _norm(c)
        return k.endswith(_NAME_CN_EN_SUFFIX)

    # 1) 列名精确就是 name（朴素导出里「姓名」列就叫 name，最常见也最可靠）
    for c in cands:
        if _norm(c) == "name":
            return c, "列名即 name，朴素人员导出的惯用命名"
    # 2) 强特征词（employee_name / 姓名 …）：必须排除主管/部门/岗位类列，
    #    否则「主管姓名」会在排序上先于「姓名」被选中，实体名变成上级的名字。
    #    同一档内按区分度取最像「一行一个」的那列。
    strong = [c for c in cands
              if any(w in _norm(c) for w in _NAME_STRONG)
              and not any(x in _norm(c) for x in _NAME_EXCLUDE)]
    if strong:
        best = sorted(strong, key=lambda c: (-prof[c]["distinct"], c))[0]
        return best, "列名含姓名特征词，且取值区分度最高"
    # 3) 含 name 且不属于部门/组织/事件/属性展示名
    pool3 = [c for c in cands
             if "name" in _norm(c) and not any(x in _norm(c) for x in _NAME_EXCLUDE)
             and not attr_display(c)]
    if pool3:
        best = sorted(pool3, key=lambda c: (-prof[c]["distinct"], c))[0]
        return best, "列名含 name，且不属于部门/事件/属性展示名类"
    # 4) 值启发：短中文串是单行 / 少行文件里最可靠的人名信号
    for c in cands:
        if attr_display(c) or any(x in _norm(c) for x in _NAME_EXCLUDE):
            continue
        if all(_NAME_CN_RE.match(s) for s in prof[c]["samples"] if s):
            return c, "采样值均为 2~4 个纯汉字，符合人名形态"
    # 5) 属性展示名兜底（低置信）：前面全都落空才用
    for c in cands:
        if attr_display(c):
            return c, "仅有「属性中英文名」式列可选，需你确认是否为名称列"
    # 6) 文本短串 + 区分度
    pool = [c for c in cands
            if prof[c]["num_rate"] <= 0.3 and prof[c]["date_rate"] <= 0.3
            and 1.0 <= prof[c]["avg_len"] <= 20 and prof[c]["distinct"] > 2]
    if pool:
        # 取区分度最高者：人名应接近「一行一个」，部门只有两三个值
        best = sorted(pool, key=lambda c: (-prof[c]["distinct"], c))[0]
        return best, "文本短串且取值几乎与主键一一对应"
    return pk, "未找到可靠名称列，退回主键值作实体名"


def _pick_entity_type(pk: str, source_ref: dict) -> tuple[str, str]:
    pk_key = _norm(pk)
    table_key = _norm(str(source_ref.get("table") or ""))
    # 主键列名是实体归属的强信号：dept_employee.csv 里的「员工ID」是员工，不是部门。
    # 人员类主键必须先于文件名/表名里的 dept/org 命中，否则「部门员工表」会被判成组织。
    if any(w in pk_key for w in _PK_STRONG + ("员工", "人员", "职工", "姓名")):
        return "Person", "主键列名是员工标识词（优先于文件名/表名）"
    key = pk_key + table_key
    if any(w in key for w in ("dept", "org", "部门", "组织")):
        return "Organization", "主键列名/来源表指向组织"
    if any(w in key for w in ("project", "项目")):
        return "Project", "主键列名/来源表指向项目"
    return "Person", "员工/人员类主键的默认类型"


def _dict_hit(column: str):
    key = _norm(column)
    # 组织层级列：dept_cn_name_lv3 / dept_org_lv2 … → department-N，
    # 每层一条事实；这是 HR 导出的通用模式，比泛 dept 命中更精确。
    m = re.search(r"(?:dept|department|org|组织).*lv(\d+)$", key)
    if m:
        return {"predicate": f"department-{m.group(1)}", "object_kind": "literal",
                "target_entity_type": "", "relation_type": "", "single": True,
                "keyword": f"组织层级 lv{m.group(1)}"}
    for keywords, predicate, kind, target, relation, single in _COLUMN_DICT:
        for kw in keywords:
            if kw in key:
                return {"predicate": predicate, "object_kind": kind,
                        "target_entity_type": target, "relation_type": relation,
                        "single": single, "keyword": kw}
    return None


# 名称类 literal 谓词：值必须是「可读名称」。布尔/编码/日期/数字形态的列
# 命中这些谓词时一律排除——dept_entry_dt（日期）写 department、
# ts_position_seq（序号）写 job_title 都是脏事实。
_NAMEY_PREDICATES = {"department", "job_title", "level", "status",
                     "location", "works_at", "name"}


def _is_namey(pred: str) -> bool:
    """名称类谓词：值必须是可读名称。department-N（层级）同属此类。"""
    return pred in _NAMEY_PREDICATES or pred.startswith("department-")


def _pred_rank(column: str) -> int:
    """冲突裁决排序：列名越短越直接——hr_status 比 marital_status_cn_name
    更配 status，city 比 work_location_odps_cn_name 更配 location，
    dept_cn_name(11) 比 dept_cn_name_end(14) 更配 department。
    不分 cn/en 档：dept_cn_name 恰是想要的部门名，按后缀降档会误伤。"""
    return len(_norm(column))


def _unfit_for_namey(prof: dict[str, dict], column: str, kind: str) -> str:
    """返回不适写名称类事实的原因；空串表示适合。只挡 literal。"""
    if kind != "literal":
        return ""
    key = _norm(column)
    if _is_bool_col(prof, column):
        return "布尔值列"
    if key.endswith(("code", "seq", "no", "num", "id", "编码", "编号", "序号")):
        return "编码/序号列"
    if prof[column]["date_rate"] >= 0.6:
        return "日期列"
    if prof[column]["num_rate"] > 0.8:
        return "数字列"
    return ""


def _is_sensitive(column: str) -> bool:
    """英文按完整词段判定，中文按子串判定。

    裸子串匹配会把 manager_id（含 age）、hotel（含 tel）判成敏感列，
    进而静默丢掉真实关系字段——这类误判比漏判更难发现，必须按词段判。
    """
    words = set(_tokens(column))
    flat = _norm(column)
    key = str(column or "").lower()
    for hint in _SENSITIVE_HINTS:
        if hint.isascii():
            # 短词（age / home）只认整词，长词（idcard / workphone）允许粘连匹配
            if hint in words or (len(hint) >= 5 and hint in flat):
                return True
        elif hint in key:
            return True
    return False


# 布尔值集合：样例全部落在这里的列视为布尔列，不写名称类事实
_BOOL_VALUES = {"y", "n", "yes", "no", "true", "false", "t", "f", "0", "1",
                "是", "否"}


def _is_bool_col(prof: dict[str, dict], column: str) -> bool:
    vals = [s.lower() for s in prof[column]["samples"] if s]
    return bool(vals) and all(v in _BOOL_VALUES for v in vals)


def _tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name or ""))
    return [p.lower() for p in re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", spaced) if p]


def _looks_entity_ref(prof: dict[str, dict], column: str) -> bool:
    """值形态像「指向另一个实体」：高基数、短串、且不是日期/数字主值。"""
    p = prof[column]
    return (p["distinct"] > 1 and p["avg_len"] <= 40 and p["date_rate"] < 0.5)


def _date_column_roles(prof: dict[str, dict], columns: list[str]) -> tuple[str, str]:
    from_col = to_col = ""
    for c in columns:
        key = _norm(c)
        if prof[c]["date_rate"] < 0.6:
            continue
        if any(w in key for w in ("start", "from", "begin", "effective", "生效", "开始", "起始")):
            from_col = from_col or c
        elif any(w in key for w in ("end", "to", "expire", "失效", "结束", "终止")):
            to_col = to_col or c
    return from_col, to_col


def suggest_mapping(*, rows: list[dict], source_ref: dict | None = None,
                    use_llm: bool = True,
                    column_order: list[str] | None = None) -> dict:
    """纯函数主体：给定源行，返回建议契约 + 逐列依据。不写库。

    column_order 由调用方从上传快照的 columns 传入：行数据经 JSONB 往返后键序
    不再可信，用文件解析时的原始列序展示才与用户看到的表格一致。
    """
    source_ref = source_ref or {}
    prof = profile_columns(rows)
    if not prof:
        return {"error": "empty_rows", "errors": ["没有可分析的行，请先上传或粘贴数据"]}
    if column_order:
        head = [str(c) for c in column_order if str(c) in prof]
        prof = {**{c: prof[c] for c in head},
                **{c: v for c, v in prof.items() if c not in set(head)}}
    columns = list(prof.keys())
    pk, pk_reason = _pick_pk(prof, columns)
    name_col, name_reason = _pick_name(prof, columns, pk)
    entity_type, type_reason = _pick_entity_type(pk, source_ref)
    valid_from, valid_to = _date_column_roles(prof, columns)

    notes: list[str] = []
    advice: list[dict] = []
    used_predicates: dict[str, str] = {}
    items_by_col: dict[str, dict] = {}     # 列 → 建议项引用（冲突裁决时降级用）
    pending_llm: list[str] = []

    for c in columns:
        p = prof[c]
        item = {
            "column": c,
            "role": "pk" if c == pk else ("name" if c == name_col else "field"),
            "predicate": "", "object_kind": "literal", "target_entity_type": "",
            "relation_type": "", "visibility": "owner", "enabled": True,
            "required": p["nonnull_rate"] >= 1.0,
            "sample": (p["samples"] or [""])[0],
            # 确认卡要让人一眼看出「主键是一列、不是某一个值」：给多个样例 +
            # 抽样内的取值个数与非空率，避免把单个样例值误读成「只有一条数据」。
            "samples": p["samples"][:4],
            "nonnull_rate": p["nonnull_rate"],
            "distinct": p["distinct"],
            "unique": p["unique"],
            "source": "rule", "confidence": 0.0, "reason": "",
        }
        if c == pk:
            item.update({"enabled": False, "reason": f"主键锚点：{pk_reason}", "confidence": 1.0})
            advice.append(item)
            continue
        if c == name_col:
            item.update({"enabled": False, "reason": f"实体名称：{name_reason}", "confidence": 1.0})
            advice.append(item)
            continue
        if c in (valid_from, valid_to):
            item.update({"enabled": False, "confidence": 1.0,
                         "reason": "时间列：已绑定为有效区间，不单独入事实"})
            advice.append(item)
            continue
        if p["nonnull_rate"] == 0:
            item.update({"enabled": False, "confidence": 0.6,
                         "reason": "整列为空，无值可写；已默认排除"})
            advice.append(item)
            continue

        hit = _dict_hit(c)
        sensitive = _is_sensitive(c)
        # 值形态守卫：布尔/编码/日期/数字列命中「名称类」谓词时排除——
        # dept_entry_dt 抢 department、ts_position_seq 抢 job_title 都会把
        # 真正的名称列挤成「谓词已被占用」。
        unfit = ""
        if hit and hit["predicate"] and _is_namey(hit["predicate"]):
            unfit = _unfit_for_namey(prof, c, hit["object_kind"])
        if hit and unfit:
            item.update({"enabled": False, "confidence": 0.4,
                         "reason": f"词典疑似命中「{hit['keyword']}」，但这是{unfit}，"
                                   "写入名称类事实没有意义；已默认排除，可手动开启"})
            advice.append(item)
            continue
        if hit and hit["predicate"]:
            pred = hit["predicate"]
            if pred in used_predicates:
                prev_col = used_predicates[pred]
                # 冲突裁决：不看列序，看「谁更直接」。朴素列 > cn_name > en_name，
                # 同档取列名更短者——hr_status 比 marital_status_cn_name 更配
                # status，city 比 work_location_odps_en_name 更配 location。
                if _pred_rank(c) >= _pred_rank(prev_col):
                    item.update({"enabled": False, "confidence": 0.5,
                                 "reason": f"谓词「{pred}」由更直接的列 {prev_col} 承担，"
                                           "本列已排除；如需换绑可手动修改"})
                    advice.append(item)
                    continue
                prev_item = items_by_col[prev_col]
                prev_item.update({"enabled": False, "confidence": 0.5,
                                  "reason": f"谓词「{pred}」改由更直接的列 {c} 承担；本列已排除"})
                kind = hit["object_kind"]
                if kind == "entity_ref" and not _looks_entity_ref(prof, c):
                    kind = "literal"
                item.update({
                    "predicate": pred, "object_kind": kind,
                    "target_entity_type": hit["target_entity_type"] if kind == "entity_ref" else "",
                    "relation_type": hit["relation_type"] if kind == "entity_ref" else "",
                    "confidence": 0.9,
                    "reason": f"列名词典命中「{hit['keyword']}」，列名比 {prev_col} 更直接",
                })
                if sensitive:
                    item.update({"visibility": "restricted", "enabled": False,
                                 "reason": item["reason"] + "；同时命中敏感词，默认不落库"})
                    notes.append(f"{c}：命中敏感词，已默认排除（绩效/薪资类需先完成合规擦除方案）")
                used_predicates[pred] = c
                items_by_col[c] = item
                advice.append(item)
                continue
            used_predicates[pred] = c
            kind = hit["object_kind"]
            if kind == "entity_ref" and not _looks_entity_ref(prof, c):
                kind = "literal"     # 值不是指向别的实体，别硬当关系
            item.update({
                "predicate": pred, "object_kind": kind,
                "target_entity_type": hit["target_entity_type"] if kind == "entity_ref" else "",
                "relation_type": hit["relation_type"] if kind == "entity_ref" else "",
                "confidence": 0.9,
                "reason": f"列名词典命中「{hit['keyword']}」",
            })
            if sensitive:
                item.update({"visibility": "restricted", "enabled": False,
                             "reason": item["reason"] + "；同时命中敏感词，默认不落库"})
                notes.append(f"{c}：命中敏感词，已默认排除（绩效/薪资类需先完成合规擦除方案）")
            items_by_col[c] = item
            advice.append(item)
            continue

        if sensitive:
            item.update({"enabled": False, "visibility": "restricted", "confidence": 0.85,
                         "reason": "命中敏感词表，默认不落库"})
            advice.append(item)
            continue

        # 词典未命中：先看值形态能不能直接判定为实体引用，否则留给模型/人工
        if _looks_entity_ref(prof, c) and p["distinct"] > 3 and p["avg_len"] <= 20:
            pending_llm.append(c)
            item.update({"confidence": 0.0, "reason": "待定：列名未命中词典",
                         "enabled": False, "object_kind": "entity_ref",
                         "target_entity_type": entity_type})
        else:
            pending_llm.append(c)
            item.update({"confidence": 0.0, "reason": "待定：列名未命中词典", "enabled": False})
        advice.append(item)

    llm_meta = {"used": False, "error": "", "asked": 0}
    if use_llm and pending_llm:
        filled = _ask_llm(pending_llm, prof, entity_type)
        llm_meta["asked"] = min(len(pending_llm), _LLM_MAX_COLUMNS)
        if filled.get("error"):
            llm_meta["error"] = filled["error"]
            notes.append("模型建议不可用（已按「默认不入库」处理，可手填谓词后重试）："
                         + str(filled["error"])[:160])
        else:
            llm_meta["used"] = bool(filled.get("items"))
            by_col = {i["column"]: i for i in filled.get("items") or []}
            for item in advice:
                got = by_col.get(item["column"])
                if not got or not got.get("predicate"):
                    continue
                pred = str(got["predicate"]).strip()
                if not re.fullmatch(r"[a-z][a-z0-9_]{1,48}", pred):
                    continue
                if pred in used_predicates:
                    continue
                used_predicates[pred] = item["column"]
                kind = "entity_ref" if got.get("object_kind") == "entity_ref" else "literal"
                if kind == "entity_ref" and not item.get("target_entity_type"):
                    kind = "literal"
                item.update({
                    "predicate": pred, "object_kind": kind,
                    "relation_type": pred if kind == "entity_ref" else "",
                    "target_entity_type": entity_type if kind == "entity_ref" else "",
                    "confidence": _LLM_CONFIDENCE, "enabled": True, "source": "llm",
                    "reason": "模型建议（需你确认；不确认则不入库）",
                })
            if llm_meta["used"]:
                notes.append("模型只给候选谓词，写入仍由确定性断言执行；"
                             "未确认的建议列不会进入契约")

    enabled = [a for a in advice if a["enabled"] and a["predicate"]]
    fields = []
    for a in enabled:
        field = {
            "column": a["column"],
            "predicate": a["predicate"],
            "object_kind": a["object_kind"],
            "target_entity_type": a["target_entity_type"],
            "relation_type": a["relation_type"],
            "target_name_column": "",
            "visibility": a["visibility"],
            "valid_from_column": valid_from,
            "valid_to_column": valid_to,
            "required": prof[a["column"]]["nonnull_rate"] >= 1.0,
        }
        fields.append(field)

    mapping = {
        "entity": {
            "entity_type": entity_type,
            "pk_column": pk,
            "name_column": name_col,
            "external_id_prefix": _prefix_for(entity_type, source_ref),
        },
        "fields": fields,
        "sensitivity": {
            "default": "owner",
            "restricted_columns": [a["column"] for a in advice
                                   if a["visibility"] == "restricted"],
        },
        "allowed_values": {},
        "expect_columns": columns,
    }
    unresolved = [a["column"] for a in advice if not a["predicate"] and a["role"] == "field"
                  and not a["enabled"] and "敏感" not in a["reason"]
                  and "待定" in a["reason"]]
    if unresolved:
        notes.append(f"有 {len(unresolved)} 列没有谓词（默认不入库）："
                     + "、".join(unresolved[:8]) + ("…" if len(unresolved) > 8 else ""))

    return {
        "mapping": mapping,
        "entity": {"pk_column": pk, "name_column": name_col,
                   "entity_type": entity_type, "valid_from_column": valid_from,
                   "valid_to_column": valid_to,
                   "reason": f"{pk_reason}；{name_reason}；{type_reason}"},
        "columns": advice,
        "unresolved": unresolved,
        "llm": llm_meta,
        "notes": notes,
        "profiled_rows": min(len(rows), _sample_size(len(columns), len(rows))),
        "total_rows": len(rows),
    }


def _prefix_for(entity_type: str, source_ref: dict) -> str:
    system = _norm(str(source_ref.get("system") or ""))
    if system:
        return f"{system[:8]}:"
    return {"Person": "hr:", "Organization": "org:", "Project": "prj:"}.get(
        entity_type, "ext:")


def _ask_llm(columns: list[str], prof: dict[str, dict], entity_type: str) -> dict:
    """只问「列名叫什么谓词」。任何异常都降级为空建议，不阻断主流程。"""
    asked = columns[:_LLM_MAX_COLUMNS]
    lines = []
    for c in asked:
        p = prof[c]
        lines.append(f"- {c}｜样例：{' / '.join(p['samples'][:3]) or '（空）'}"
                     f"｜平均长度 {p['avg_len']}｜不同值 {p['distinct']}")
    system = (
        "你是数据建模助手，负责把表格列映射到知识本体的谓词名。"
        "谓词用 snake_case 英文动词短语（如 department、job_title、reports_to），"
        "无法判断使用价值时给 null。object_kind 只能是 literal 或 entity_ref"
        "（entity_ref 表示该列的值指向另一个实体，例如上级工号）。"
        f"实体类型为 {entity_type}。只输出 JSON 数组，元素形如 "
        '{"column":"x","predicate":"y","object_kind":"literal","confidence":0.8}。'
    )
    user = "列清单：\n" + "\n".join(lines)
    try:
        from app.modules.llm.client import llm_client
        data = llm_client.chat_json(system, user, strict=False, timeout=25)
    except Exception as exc:                      # 端点不可用 / 超时 / 非 JSON
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}
    if isinstance(data, dict):
        data = data.get("items") or data.get("columns") or []
    items = []
    for row in data if isinstance(data, list) else []:
        if isinstance(row, dict) and row.get("column"):
            items.append({
                "column": str(row["column"]).strip(),
                "predicate": str(row.get("predicate") or "").strip(),
                "object_kind": str(row.get("object_kind") or "literal").strip(),
                "confidence": row.get("confidence") or 0.5,
            })
    return {"items": items}
