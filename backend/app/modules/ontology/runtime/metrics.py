"""本体运行时 · 标准指标算法（19 种）+ 2 个运行时补充口径。

前端属性面板的「指标函数」下拉给了 19 个标准算法名（MD_METRIC_FNS），但后端此前只有
名字、没有任何实现——选了下拉也跑不出值。本模块把这 19 个口径一一实现成纯函数：
输入是一列已抽取的值（按业务顺序），输出是标量或结构化值 + 一句口径说明。

设计约束（与项目「缺口照实披露、禁止编造」一致）：
1. 每个算法都返回 ``(value, note)``：note 说明**这一次**实际怎么算的，含跳过了多少条
   无法解析的输入。算不出来就返回 ``None`` 并把原因写进 note，不返回 0 冒充。
2. 不猜数据类型。``Sum`` 只对可解析为数字的值求和，非数字计入 skipped；
   ``TimeSeries`` 抽不到时间戳就报「无可用时间点」。
3. ``DistinctCountHllp`` 名义上是 HyperLogLog++ 近似，本地没有草图依赖，
   实现为精确去重计数——note 里明确写出「误差 0，口径一致、精度更高」，不假装是 HLL。
4. ``QuantileFloats`` / ``QuantileDoubles`` 都按双精度算（Python float 即 double），
   note 里写明；``QuantileStrings`` 用「排序后取 round(q*(n-1)) 下标」口径。

另有两个**运行时补充口径**（不属于 19 种标准算法，但前端兜底规则会生成这类逻辑，
不实现就必然「建模能选、运行时算不出」）：
- ``RowCount``：实例数（对应 ``count(*)``）
- ``FillRate``：非空率（对应 ``sum(case when x is not null ...)/count(*)``）

全部纯函数，零 I/O。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

from .expr import _num  # 复用同一套「字符串→数字」口径，避免两处漂移

_DATE_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?)?")


# ---------------------------------------------------------------- 通用工具
def _blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) == 0
    return False


def _nonnull(values: list[Any]) -> list[Any]:
    return [v for v in values if not _blank(v)]


def _nums(values: list[Any]) -> tuple[list[float], int]:
    """抽出可解析为数字的值；返回 (数字列表, 跳过条数)。"""
    nums: list[float] = []
    skipped = 0
    for v in values:
        if _blank(v):
            skipped += 1
            continue
        n = _num(v)
        if n is None:
            skipped += 1
            continue
        nums.append(float(n))
    return nums, skipped


def _ts_key(v: Any) -> datetime | None:
    """把日期/时间/时间戳/ISO 串归一成可比较的 UTC datetime；解析不了返回 None。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if not s:
        return None
    m = _DATE_RE.match(s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4) or 0), int(m.group(5) or 0),
                            int(m.group(6) or 0), tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _hashable(v: Any) -> Any:
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((str(k), _hashable(x)) for k, x in v.items()))
    if isinstance(v, set):
        return tuple(sorted(_hashable(x) for x in v))
    return v


def _sort_key(v: Any) -> tuple:
    ts = _ts_key(v)
    if ts is not None:
        return (0, ts, "")
    n = _num(v)
    if n is not None:
        return (1, n, "")
    return (2, None, str(v))


def _as_items(v: Any) -> list[Any]:
    if isinstance(v, (list, tuple, set)):
        return list(v)
    return [v]


def _quantile(nums: list[float], q: float) -> float | None:
    """线性插值分位（与 numpy.percentile 默认口径一致）。"""
    if not nums:
        return None
    xs = sorted(nums)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def _q_of(opts: dict | None) -> float:
    raw = (opts or {}).get("q", 0.5)
    n = _num(raw)
    if n is None:
        return 0.5
    return min(1.0, max(0.0, float(n)))


# ---------------------------------------------------------------- 19 种标准算法
def _ordered(values: list[Any], opts: dict, *, last: bool) -> tuple[Any, str]:
    vals = _nonnull(values)
    if not vals:
        return None, "无有效值（整列为空或全为 null）"
    order_by = str((opts or {}).get("order_by") or "").strip()
    if order_by and all(isinstance(v, dict) for v in vals):
        keyed = []
        for v in vals:
            k = _sort_key(v.get(order_by))
            if k[0] == 2:  # 既解析不出时间也解析不出数字，跳过该条
                continue
            keyed.append((k, v))
        if keyed:
            keyed.sort(key=lambda kv: kv[0])
            pick = "最新" if last else "最早"
            return keyed[-1 if last else 0][1], \
                f"按 {order_by} 排序取{pick}（{len(keyed)} 条可排序）"
        return None, f"按 {order_by} 排序时没有任何一条可解析的值"
    tail = "最后一条" if last else "第一条"
    return (vals[-1] if last else vals[0]), (
        f"未提供 order_by，按输入顺序取{tail}（顺序即业务时间顺序）")


def _ordered_last(values: list[Any], opts: dict) -> tuple[Any, str]:
    return _ordered(values, opts, last=True)


def _ordered_first(values: list[Any], opts: dict) -> tuple[Any, str]:
    return _ordered(values, opts, last=False)


def _sum(values: list[Any], opts: dict) -> tuple[Any, str]:
    nums, skipped = _nums(values)
    if not nums:
        return None, f"没有可求和的数字（跳过 {skipped} 条）"
    total = sum(nums)
    return (int(total) if float(total).is_integer() else total), \
        f"{len(nums)} 个数字求和" + (f"，跳过 {skipped} 条非数字/空值" if skipped else "")


def _avg(values: list[Any], opts: dict) -> tuple[Any, str]:
    nums, skipped = _nums(values)
    if not nums:
        return None, f"没有可求平均的数字（跳过 {skipped} 条）"
    return sum(nums) / len(nums), \
        f"{len(nums)} 个数字取平均" + (f"，跳过 {skipped} 条" if skipped else "")


def _min(values: list[Any], opts: dict) -> tuple[Any, str]:
    vals = _nonnull(values)
    if not vals:
        return None, "无有效值"
    nums, skipped = _nums(vals)
    if nums and skipped == 0:
        v = min(nums)
        return (int(v) if float(v).is_integer() else v), f"{len(nums)} 个数字取最小"
    return min(vals, key=_sort_key), f"{len(vals)} 个值按时间/数字/文本顺序取最小"


def _max(values: list[Any], opts: dict) -> tuple[Any, str]:
    vals = _nonnull(values)
    if not vals:
        return None, "无有效值"
    nums, skipped = _nums(vals)
    if nums and skipped == 0:
        v = max(nums)
        return (int(v) if float(v).is_integer() else v), f"{len(nums)} 个数字取最大"
    return max(vals, key=_sort_key), f"{len(vals)} 个值按时间/数字/文本顺序取最大"


def _product(values: list[Any], opts: dict) -> tuple[Any, str]:
    nums, skipped = _nums(values)
    if not nums:
        return None, f"没有可连乘的数字（跳过 {skipped} 条）"
    out = 1.0
    for n in nums:
        out *= n
        if not math.isfinite(out):
            return None, "连乘溢出（结果为 inf），请检查数值量级"
    return (int(out) if float(out).is_integer() else out), \
        f"{len(nums)} 个数字连乘" + (f"，跳过 {skipped} 条" if skipped else "")


def _str_concat(values: list[Any], opts: dict) -> tuple[Any, str]:
    sep = str((opts or {}).get("sep") if (opts or {}).get("sep") is not None else ",")
    vals = [str(v) for v in _nonnull(values)]
    if not vals:
        return None, "无有效值"
    return sep.join(vals), f"{len(vals)} 个值按输入顺序拼接（分隔符 {sep!r}）"


def _str_concat_dedup(values: list[Any], opts: dict) -> tuple[Any, str]:
    sep = str((opts or {}).get("sep") if (opts or {}).get("sep") is not None else ",")
    seen: dict[str, int] = {}
    for v in _nonnull(values):
        s = str(v)
        seen[s] = seen.get(s, 0) + 1
    if not seen:
        return None, "无有效值"
    dup = sum(c - 1 for c in seen.values())
    return sep.join(seen.keys()), \
        f"{len(seen)} 个去重值拼接（保留首次出现顺序，剔除重复 {dup} 条）"


def _collection_concat(values: list[Any], opts: dict) -> tuple[Any, str]:
    vals = _nonnull(values)
    if not vals:
        return None, "无有效值"
    out: list[Any] = []
    for v in vals:
        out.extend(_as_items(v))
    return out, f"{len(vals)} 个集合/标量拼成 {len(out)} 个元素（保留重复）"


def _collection_intersect(values: list[Any], opts: dict) -> tuple[Any, str]:
    vals = _nonnull(values)
    if not vals:
        return None, "无有效值"
    lists = [_as_items(v) for v in vals]
    first = lists[0]
    others = [set(_hashable(x) for x in lst) for lst in lists[1:]]
    out: list[Any] = []
    seen: set[Any] = set()
    for item in first:
        h = _hashable(item)
        if h in seen:
            continue
        if all(h in s for s in others):
            seen.add(h)
            out.append(item)
    return out, (f"{len(lists)} 个集合求交，结果 {len(out)} 个元素"
                 + ("" if others else "（只有 1 个集合，交集即其自身去重）"))


def _freq_map(values: list[Any], opts: dict) -> tuple[Any, str]:
    vals = _nonnull(values)
    if not vals:
        return None, "无有效值"
    cnt = Counter(_hashable(v) for v in vals)
    orig: dict[Any, Any] = {}
    for v in vals:
        orig.setdefault(_hashable(v), v)
    ordered = sorted(cnt.items(), key=lambda kv: (-kv[1], str(kv[0])))
    out = [{"value": orig[k], "count": c} for k, c in ordered]
    return out, f"{len(vals)} 个值归成 {len(out)} 个取值（按出现次数降序）"


def _distinct_count(values: list[Any], opts: dict) -> tuple[Any, str]:
    vals = _nonnull(values)
    if not vals:
        return None, "无有效值"
    n = len({_hashable(v) for v in vals})
    return n, (f"{len(vals)} 个值去重后 {n} 个。"
               "本地实现为精确去重计数（未使用 HyperLogLog++ 草图，误差 0，口径一致、精度更高）")


def _quantile_numeric(values: list[Any], opts: dict, *, label: str) -> tuple[Any, str]:
    nums, skipped = _nums(values)
    q = _q_of(opts)
    v = _quantile(nums, q)
    if v is None:
        return None, f"没有可算分位的数字（跳过 {skipped} 条）"
    return v, (f"{len(nums)} 个数字取 {q:g} 分位（线性插值）"
               + (f"，跳过 {skipped} 条" if skipped else "")
               + f"；{label} 按双精度计算")


def _quantile_floats(values: list[Any], opts: dict) -> tuple[Any, str]:
    return _quantile_numeric(values, opts, label="QuantileFloats")


def _quantile_doubles(values: list[Any], opts: dict) -> tuple[Any, str]:
    return _quantile_numeric(values, opts, label="QuantileDoubles")


def _quantile_strings(values: list[Any], opts: dict) -> tuple[Any, str]:
    vals = [str(v) for v in _nonnull(values)]
    if not vals:
        return None, "无有效值"
    q = _q_of(opts)
    xs = sorted(vals)
    idx = int(round(q * (len(xs) - 1)))
    idx = min(max(idx, 0), len(xs) - 1)
    return xs[idx], (f"{len(xs)} 个字符串字典序排序后取 {q:g} 分位（下标 "
                     f"round({q:g}×{len(xs) - 1})={idx}）")


def _time_series(values: list[Any], opts: dict) -> tuple[Any, str]:
    o = opts or {}
    ts_field = str(o.get("ts_field") or o.get("order_by") or "ts")
    val_field = str(o.get("value_field") or "value")
    points: list[tuple[datetime, Any]] = []
    bad = 0
    for v in values:
        if isinstance(v, dict):
            k = _ts_key(v.get(ts_field))
            val = v.get(val_field)
        elif isinstance(v, (list, tuple)) and len(v) >= 2:
            k = _ts_key(v[0])
            val = v[1]
        else:
            k = None
            val = None
        if k is None:
            bad += 1
            continue
        points.append((k, val))
    if not points:
        return None, (f"无可用时间点：需要 dict{{'{ts_field}','{val_field}'}} 或 "
                      f"[时间, 值] 形式，实际收到 {len(values)} 条都无法解析时间")
    points.sort(key=lambda p: p[0])
    return [[p[0].isoformat(), p[1]] for p in points], \
        f"{len(points)} 个时间点按时间升序" + (f"，{bad} 条无法解析时间被跳过" if bad else "")


def _timestamp_set(values: list[Any], opts: dict) -> tuple[Any, str]:
    seen: dict[datetime, Any] = {}
    bad = 0
    for v in _nonnull(values):
        k = _ts_key(v)
        if k is None:
            bad += 1
            continue
        seen.setdefault(k, v)
    if not seen:
        return None, f"无可用时间戳（{bad} 条都无法解析为时间）"
    out = [seen[k] for k in sorted(seen)]
    return out, (f"{len(out)} 个去重时间戳按时间升序"
                 + (f"，{bad} 条无法解析被跳过" if bad else ""))


def _timestamp_set_bounded(values: list[Any], opts: dict) -> tuple[Any, str]:
    o = opts or {}
    start = _ts_key(o.get("start")) if o.get("start") not in (None, "") else None
    end = _ts_key(o.get("end")) if o.get("end") not in (None, "") else None
    kept: dict[datetime, Any] = {}
    bad = 0
    dropped = 0
    for v in _nonnull(values):
        k = _ts_key(v)
        if k is None:
            bad += 1
            continue
        if start is not None and k < start:
            dropped += 1
            continue
        if end is not None and k > end:
            dropped += 1
            continue
        kept.setdefault(k, v)
    if not kept:
        return None, (f"边界内无时间戳（解析失败 {bad} 条，越界剔除 {dropped} 条；"
                      f"边界 start={o.get('start') or '无'} end={o.get('end') or '无'}）")
    out = [kept[k] for k in sorted(kept)]
    if start is None and end is None:
        note = (f"{len(out)} 个去重时间戳；未提供 start/end 边界，按无界集合处理"
                "（等同 TimestampSet）")
    else:
        note = (f"边界内 {len(out)} 个去重时间戳（start={o.get('start') or '-∞'}，"
                f"end={o.get('end') or '+∞'}，越界剔除 {dropped} 条）")
    if bad:
        note += f"；{bad} 条无法解析被跳过"
    return out, note


# ---------------------------------------------------------------- 运行时补充口径
def _row_count(values: list[Any], opts: dict) -> tuple[Any, str]:
    return len(values), f"实例数 {len(values)}（统计输入行数，不看空值）"


def _fill_rate(values: list[Any], opts: dict) -> tuple[Any, str]:
    if not values:
        return None, "没有输入行，非空率无定义"
    filled = len(_nonnull(values))
    rate = filled / len(values)
    return round(rate, 6), f"非空率 {filled}/{len(values)} = {rate:.2%}"


# ---------------------------------------------------------------- 注册表
_ALGO: Callable[[list[Any], dict], tuple[Any, str]]

METRIC_ALGOS: dict[str, Callable[[list[Any], dict], tuple[Any, str]]] = {
    "OrderedLast": _ordered_last,
    "OrderedFirst": _ordered_first,
    "Sum": _sum,
    "Avg": _avg,
    "Min": _min,
    "Max": _max,
    "Product": _product,
    "StrConcat": _str_concat,
    "StrConcatDeduplicate": _str_concat_dedup,
    "CollectionConcat": _collection_concat,
    "CollectionIntersect": _collection_intersect,
    "FreqMap": _freq_map,
    "DistinctCountHllp": _distinct_count,
    "QuantileFloats": _quantile_floats,
    "QuantileDoubles": _quantile_doubles,
    "QuantileStrings": _quantile_strings,
    "TimeSeries": _time_series,
    "TimestampSet": _timestamp_set,
    "TimestampSetBounded": _timestamp_set_bounded,
}

PSEUDO_ALGOS: dict[str, Callable[[list[Any], dict], tuple[Any, str]]] = {
    "RowCount": _row_count,
    "FillRate": _fill_rate,
}

ALL_ALGOS = {**METRIC_ALGOS, **PSEUDO_ALGOS}

# 口径目录：前端、MCP「能力自述」与文档共用，避免三处漂移
METRIC_CATALOG: list[dict[str, str]] = [
    {"algo": "OrderedLast", "label": "按业务时间取最新", "family": "ordered", "needs": "any"},
    {"algo": "OrderedFirst", "label": "按业务时间取最早", "family": "ordered", "needs": "any"},
    {"algo": "Sum", "label": "求和", "family": "numeric", "needs": "numeric"},
    {"algo": "Avg", "label": "平均", "family": "numeric", "needs": "numeric"},
    {"algo": "Min", "label": "最小", "family": "numeric", "needs": "numeric"},
    {"algo": "Max", "label": "最大", "family": "numeric", "needs": "numeric"},
    {"algo": "Product", "label": "连乘", "family": "numeric", "needs": "numeric"},
    {"algo": "StrConcat", "label": "字符串拼接", "family": "string", "needs": "any"},
    {"algo": "StrConcatDeduplicate", "label": "去重拼接", "family": "string", "needs": "any"},
    {"algo": "CollectionConcat", "label": "集合拼接", "family": "collection", "needs": "collection"},
    {"algo": "CollectionIntersect", "label": "集合求交", "family": "collection", "needs": "collection"},
    {"algo": "FreqMap", "label": "频次统计", "family": "distribution", "needs": "any"},
    {"algo": "DistinctCountHllp", "label": "近似去重计数（本地为精确）", "family": "count", "needs": "any"},
    {"algo": "QuantileFloats", "label": "分位数（浮点）", "family": "quantile", "needs": "numeric"},
    {"algo": "QuantileDoubles", "label": "分位数（双精度）", "family": "quantile", "needs": "numeric"},
    {"algo": "QuantileStrings", "label": "分位数（字符串）", "family": "quantile", "needs": "any"},
    {"algo": "TimeSeries", "label": "时间序列", "family": "temporal", "needs": "timestamp"},
    {"algo": "TimestampSet", "label": "时间戳集合", "family": "temporal", "needs": "timestamp"},
    {"algo": "TimestampSetBounded", "label": "有界时间戳集合", "family": "temporal", "needs": "timestamp"},
    {"algo": "RowCount", "label": "实例数（运行时补充口径）", "family": "count", "needs": "any"},
    {"algo": "FillRate", "label": "非空率（运行时补充口径）", "family": "count", "needs": "any"},
]

_LABEL = {x["algo"]: x["label"] for x in METRIC_CATALOG}


def algo_label(algo: str) -> str:
    return _LABEL.get(algo, algo)


def is_official(algo: str) -> bool:
    return algo in METRIC_ALGOS


def apply_metric(algo: str, values: list[Any] | None,
                 opts: dict | None = None) -> dict[str, Any]:
    """按算法名算一个指标值。未知算法不猜，直接 ok=False。"""
    name = str(algo or "").strip()
    vals = list(values or [])
    o = dict(opts or {})
    fn = ALL_ALGOS.get(name)
    if fn is None:
        return {"algo": name, "ok": False, "value": None,
                "note": f"未知指标算法 {name!r}；可用算法见 metric-catalog",
                "input": len(vals), "used": 0, "skipped": len(vals),
                "official": False, "label": name}
    try:
        value, note = fn(vals, o)
    except Exception as exc:  # noqa: BLE001  单个算法异常不该炸掉整轮运行
        return {"algo": name, "ok": False, "value": None,
                "note": f"算法执行异常：{type(exc).__name__}: {exc}",
                "input": len(vals), "used": 0, "skipped": len(vals),
                "official": is_official(name), "label": algo_label(name)}
    used = len(vals) if name == "RowCount" else len(_nonnull(vals))
    return {"algo": name, "ok": value is not None, "value": value, "note": note,
            "input": len(vals), "used": used, "skipped": len(vals) - used,
            "official": is_official(name), "label": algo_label(name)}


def self_check() -> dict[str, Any]:
    """21 个口径（19 标准 + 2 补充）逐项自检。数据本地构造，不代表业务真值。"""
    fails: list[str] = []

    def chk(name: str, values: list[Any], expect: Any,
            opts: dict | None = None) -> None:
        r = apply_metric(name, values, opts)
        if r["value"] != expect:
            fails.append(f"{name}: 期望 {expect!r} 实际 {r['value']!r}（{r['note']}）")

    chk("OrderedLast", ["a", "b", "c"], "c")
    chk("OrderedFirst", ["a", "b", "c"], "a")
    chk("OrderedLast", [{"ts": "2024-01-02", "v": 2}, {"ts": "2024-03-01", "v": 3},
                        {"ts": "2024-02-01", "v": 9}], {"ts": "2024-03-01", "v": 3},
        {"order_by": "ts"})
    chk("Sum", [1, "2", 3], 6)
    chk("Sum", ["a", None], None)
    chk("Avg", [1, 2, 3], 2.0)
    chk("Min", [3, 1, 2], 1)
    chk("Max", [3, 1, 2], 3)
    chk("Product", [2, 3, 4], 24)
    chk("StrConcat", ["a", "b", "c"], "a,b,c")
    chk("StrConcat", ["a", "b"], "a|b", {"sep": "|"})
    chk("StrConcatDeduplicate", ["a", "b", "a"], "a,b")
    chk("CollectionConcat", [[1, 2], [3]], [1, 2, 3])
    chk("CollectionIntersect", [[1, 2, 3], [2, 3, 4]], [2, 3])
    chk("FreqMap", ["x", "y", "x"], [{"value": "x", "count": 2},
                                     {"value": "y", "count": 1}])
    chk("DistinctCountHllp", ["a", "a", "b"], 2)
    chk("QuantileFloats", [1, 2, 3, 4], 2.5)
    chk("QuantileDoubles", [1, 2, 3, 4], 2.5, {"q": 0.5})
    chk("QuantileStrings", ["b", "a", "c"], "b")
    chk("TimeSeries", [["2024-01-02", 2], ["2024-01-01", 1]],
        [["2024-01-01T00:00:00+00:00", 1], ["2024-01-02T00:00:00+00:00", 2]])
    chk("TimestampSet", ["2024-02-01", "2024-01-01", "2024-02-01"],
        ["2024-01-01", "2024-02-01"])
    chk("TimestampSetBounded", ["2024-01-01", "2024-02-01", "2024-03-01"],
        ["2024-02-01"], {"start": "2024-01-15", "end": "2024-02-15"})
    chk("RowCount", [1, None, 3], 3)
    chk("FillRate", [1, None, 3, ""], 0.5)
    chk("Sum", [], None)
    # 未知算法必须 ok=False，不许当 0
    unknown = apply_metric("NoSuchAlgo", [1, 2])
    if unknown["ok"] is not False:
        fails.append("未知算法应 ok=False")
    if len(METRIC_ALGOS) != 19:
        fails.append(f"标准算法应为 19 种，实际 {len(METRIC_ALGOS)}")
    if len(METRIC_CATALOG) != len(ALL_ALGOS):
        fails.append(f"口径目录 {len(METRIC_CATALOG)} 与实现 {len(ALL_ALGOS)} 不一致")
    missing = [k for k in ALL_ALGOS if k not in _LABEL]
    if missing:
        fails.append(f"口径目录缺少：{missing}")
    return {"pass": not fails, "failures": fails,
            "official_algos": len(METRIC_ALGOS), "pseudo_algos": len(PSEUDO_ALGOS)}
