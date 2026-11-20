"""结构化数据源的文件解析：表格 / Markdown 表格 / JSON。

定位：文件只是「行的载体」，不是新的证据来源。解析结果必须与从湖仓取的行
完全同构（`list[dict]`），后续仍走同一套映射契约、确定性断言与 confirm 闸门，
因此这里有两条硬约束：

* **不调用 LLM、不做列名语义猜测**。字段 → 谓词仍由本人在契约里显式声明，
  否则「文件上传」就变成了绕过审核闸门的自动抽取通道。
* **不静默丢弃**。编码、表头重名、多表格、多余列、嵌套值一律记入 warnings 或
  直接报错；行数超限直接拒绝，不做隐式截断（截断会让「同步成功」变成假象）。

字节上限放宽到 100 MiB 的原因：常见组织形式里，带格式的 xlsx 往往几十 MiB 却只有
几千行，10 MiB 会在进入断言前就被挡掉。放宽字节不等于放宽语义，因此这里配了三层
内存保护——**逐行流式解析**（命中上限立即中止，不把十万行先建成 list）、
**单元格总数闸门**（行 × 列 > `MAX_CELLS` 即中止，宽表自动折算成更小的行数上限）、
以及**超限即拒绝**（绝不截断）。真正的写入规模仍由 `MAX_ROWS`/`MAX_COLUMNS` 与
confirm 闸门控制。列数上限（512）只用于拦截异常表头，不用于限制正常宽表。

本模块除 xlsx 需要 openpyxl 外无第三方依赖，解析函数全部可用纯内存用例覆盖。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re

MAX_BYTES = 100 * 1024 * 1024
# 硬上限只用于保护单进程内存（约 200k 行 ≈ 数百 MB 峰值），不是业务上限：
# 超过 BATCH_ROWS 的文件由服务端自动分批，逐批断言、逐批事务，全部行都会写入。
MAX_ROWS = 200000
BATCH_ROWS = 5000
# 列数上限只用来挡住「把非表格文件当表格解析」这类异常表头（几千列）。真实组织
# 数据导出常见 100~300 列，80 列会把正常的宽表拦在门外，因此放到 512 列。
MAX_COLUMNS = 512
# 真正吃掉内存的是「行 × 列」而不是行或列单独一项：512 列 × 20 万行 = 1 亿单元格，
# 峰值会到 GB 级。用单元格总数做硬闸门，宽表就自动折算成更小的行数上限。
MAX_CELLS = 6_000_000

TABLE_FORMATS = ("csv", "tsv", "xlsx")
SUPPORTED = (*TABLE_FORMATS, "md", "json")
FORMAT_LABEL = {"csv": "CSV 表格", "tsv": "TSV 表格", "xlsx": "Excel 工作簿",
                "md": "Markdown 表格", "json": "JSON 数组"}

_JSON_LIST_KEYS = ("rows", "data", "items", "records", "results", "list")
_MD_SEP = re.compile(r"^:?-{2,}:?$")


class LimitExceeded(ValueError):
    """超限一律中止：不截断、不降级、不落半份快照。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _too_many_rows(seen: int | None = None) -> LimitExceeded:
    # 上限在调用时读取模块常量（便于测试与后续按环境调整），不在定义时固化默认值
    limit = MAX_ROWS
    detail = f"已解析到 {seen} 行，" if seen else ""
    return LimitExceeded(
        "too_many_rows",
        f"{detail}超过单文件硬上限 {limit} 行。该上限只用于保护服务进程内存，"
        "与「自动分批」无关——常规文件超出分批阈值会自动拆批，请把文件拆成多个再上传",
    )


def _too_many_cells(rows: int, columns: int) -> LimitExceeded:
    return LimitExceeded(
        "too_many_cells",
        f"已解析到 {rows} 行 × {columns} 列 = {rows * columns:,} 个单元格，"
        f"超过单文件单元格硬上限 {MAX_CELLS:,}。该上限只用于保护服务进程内存："
        "请在契约里只保留需要的列后重新导出，或把文件拆成多个再上传",
    )


def _too_large(size: int) -> LimitExceeded:
    return LimitExceeded(
        "too_large",
        f"文件 {size / 1024 / 1024:.1f} MiB 超过上限 "
        f"{MAX_BYTES // 1024 // 1024} MiB，请拆分后上传",
    )


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def detect_format(file_name: str) -> str:
    name = str(file_name or "").strip().lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return ext if ext in SUPPORTED else ""


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码，请另存为 UTF-8 后重试")


def _text(value) -> str:
    """所有单元格统一成字符串：跨格式同构，避免 1002 与 1002.0 变成两个工号。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, "isoformat"):
        try:
            return value.date().isoformat() if hasattr(value, "date") else value.isoformat()
        except (ValueError, AttributeError):
            return str(value)
    return str(value).strip()


def _norm_columns(header: list) -> tuple[list[str], list[str]]:
    """表头规范化：去空白、补空列名、重名加后缀（重名不处理会造成列覆盖）。"""
    warnings: list[str] = []
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, cell in enumerate(header, 1):
        name = " ".join(_text(cell).split())
        if not name:
            name = f"col{i}"
            warnings.append(f"第 {i} 列表头为空，已命名为 {name}")
        if name in seen:
            seen[name] += 1
            renamed = f"{name}_{seen[name]}"
            warnings.append(f"表头「{name}」重复，第 {seen[name]} 次出现改名为 {renamed}")
            name = renamed
        else:
            seen[name] = 1
        out.append(name)
    return out, warnings


def _rows_from_matrix(matrix, columns: list[str]) -> tuple[list[dict], list[str]]:
    """逐行消费（可传生成器）：命中行数上限立即中止，避免先把大文件建成 list。"""
    warnings: list[str] = []
    rows: list[dict] = []
    extra_once = False
    # 单元格闸门先折算成行数：宽表自动变成更小的行数上限，且无需每行做乘法。
    cell_cap_rows = max(1, MAX_CELLS // max(1, len(columns)))
    for r in matrix:
        if not any(_text(c) for c in r):
            continue
        row = {columns[i]: _text(r[i]) if i < len(r) else "" for i in range(len(columns))}
        if len(r) > len(columns) and not extra_once:
            extra_once = True
            warnings.append(f"存在超出表头的 {len(r) - len(columns)} 个多余列，已忽略；"
                            "若这些列有业务含义请先整理表头")
        rows.append(row)
        if len(rows) > cell_cap_rows:
            raise _too_many_cells(len(rows), len(columns))
        if len(rows) > MAX_ROWS:
            raise _too_many_rows(seen=len(rows))
    return rows, warnings


# ---------------------------------------------------------------- 各格式解析


def _parse_delimited(text: str, delimiter: str | None) -> tuple[list[str], list[dict], list[str]]:
    sample = text[:16384]
    if not delimiter:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = "\t" if "\t" in sample.splitlines()[0] else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    it = (r for r in reader if any(_text(c) for c in r))
    header = next(it, None)
    if header is None:
        raise ValueError("文件没有可用的数据行")
    columns, warnings = _norm_columns(header)
    rows, more = _rows_from_matrix(it, columns)
    if not rows:
        raise ValueError("只解析到表头，没有数据行")
    return columns, rows, warnings + more


def _parse_xlsx(raw: bytes, sheet: str) -> tuple[list[str], list[dict], list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover - 依赖已固定
        raise ValueError("服务器缺少 openpyxl，暂不支持 xlsx") from None
    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        names = list(wb.sheetnames)
        if not names:
            raise ValueError("工作簿没有工作表")
        warnings: list[str] = []
        target = sheet if sheet and sheet in names else names[0]
        if sheet and sheet not in names:
            warnings.append(f"工作表「{sheet}」不存在，已改用「{target}」")
        elif len(names) > 1:
            warnings.append(f"工作簿含 {len(names)} 个工作表，已使用「{target}」；"
                            "其它表请另存为单独文件后上传")
        # read_only 迭代器不把整表读进内存；表头之后交给 _rows_from_matrix 流式消费。
        it = ([_text(v) for v in r] for r in wb[target].iter_rows(values_only=True))
        header = next((r for r in it if any(c for c in r)), None)
        if header is None:
            raise ValueError(f"工作表「{target}」没有数据")
        columns, w2 = _norm_columns(header)
        rows, w3 = _rows_from_matrix(it, columns)
        if not rows:
            raise ValueError("只解析到表头，没有数据行")
        return columns, rows, warnings + w2 + w3
    finally:
        wb.close()


def _split_md_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    body = body.replace("\\|", "\u0000")
    return [c.replace("\u0000", "|").strip() for c in body.split("|")]


def _parse_markdown(text: str) -> tuple[list[str], list[dict], list[str]]:
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and len(s) > 2:
            cur.append(s)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)

    tables: list[tuple[list[str], list[list[str]]]] = []
    for block in blocks:
        if len(block) < 2:
            continue
        header = _split_md_row(block[0])
        sep = _split_md_row(block[1])
        if not sep or not all(_MD_SEP.match(c.replace(" ", "")) for c in sep if c):
            continue
        body = [_split_md_row(x) for x in block[2:]]
        tables.append((header, [r for r in body if any(c for c in r)]))

    if not tables:
        raise ValueError("没有找到 Markdown 表格；请确认表头下有一行 |---|---| 分隔线")

    warnings: list[str] = []
    tables.sort(key=lambda t: len(t[1]), reverse=True)
    header, body = tables[0]
    if len(tables) > 1:
        warnings.append(f"检测到 {len(tables)} 个表格，已使用行数最多的一个"
                        f"（{len(body)} 行）；其余表格请拆成单独文件")
    columns, w2 = _norm_columns(header)
    rows, w3 = _rows_from_matrix(body, columns)
    if not rows:
        raise ValueError("Markdown 表格只有表头，没有数据行")
    return columns, rows, warnings + w2 + w3


def _flat(value, path: str, warnings: list[str]) -> str:
    if isinstance(value, (dict, list)):
        if len(warnings) < 20:
            warnings.append(f"字段 {path} 是嵌套结构，已序列化为字符串；"
                            "建议在源头拍平，否则无法作为维度值参与比较")
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _text(value)


def _parse_json(text: str) -> tuple[list[str], list[dict], list[str]]:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"JSON 解析失败：{e}") from None
    if isinstance(data, dict):
        for key in _JSON_LIST_KEYS:
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("JSON 需为对象数组，或形如 {\"rows\": [...]} 的包装")
    if len(data) > MAX_ROWS:
        raise _too_many_rows(seen=len(data))
    warnings: list[str] = []
    rows: list[dict] = []
    columns: list[str] = []
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            if len(warnings) < 20:
                warnings.append(f"第 {i} 个元素不是对象，已忽略")
            continue
        row: dict = {}
        for k, v in item.items():
            key = " ".join(str(k).split()) or f"col{len(row) + 1}"
            row[key] = _flat(v, key, warnings)
            if key not in columns:
                columns.append(key)
        rows.append(row)
    if not rows:
        raise ValueError("JSON 里没有对象行")
    if len(columns) > MAX_COLUMNS:
        raise LimitExceeded(
            "too_many_columns",
            f"解析出 {len(columns)} 列，超过列数上限 {MAX_COLUMNS}；"
            "请只导出契约里需要的字段后重试",
        )
    if len(rows) * len(columns) > MAX_CELLS:
        raise _too_many_cells(len(rows), len(columns))
    for r in rows:
        for c in columns:
            r.setdefault(c, "")
    return columns, rows, warnings


# ---------------------------------------------------------------- 对外入口


def parse_upload(file_name: str, raw: bytes, sheet: str = "",
                 delimiter: str | None = None) -> dict:
    """解析上传文件；成功返回行与列，失败返回 error/errors（不抛异常给调用方）。"""
    name = str(file_name or "").strip() or "upload"
    fmt = detect_format(name)
    if not fmt:
        return {"error": "unsupported_format",
                "errors": [f"不支持的文件类型；可用：{', '.join(SUPPORTED)}"]}
    if not raw:
        return {"error": "empty_file", "errors": ["文件为空"]}
    if len(raw) > MAX_BYTES:
        limit = _too_large(len(raw))
        return {"error": limit.code, "errors": [str(limit)]}
    try:
        if fmt in ("csv", "tsv"):
            text = _decode(raw)
            columns, rows, warnings = _parse_delimited(
                text, delimiter or ("\t" if fmt == "tsv" else None))
        elif fmt == "xlsx":
            columns, rows, warnings = _parse_xlsx(raw, sheet)
        elif fmt == "md":
            columns, rows, warnings = _parse_markdown(_decode(raw))
        else:
            columns, rows, warnings = _parse_json(_decode(raw))
    except LimitExceeded as e:            # 先于 ValueError：超限有独立错误码
        return {"error": e.code, "errors": [str(e)]}
    except ValueError as e:
        return {"error": "parse_failed", "errors": [str(e)]}

    if len(columns) > MAX_COLUMNS:
        return {"error": "too_many_columns",
                "errors": [f"解析出 {len(columns)} 列，超过列数上限 {MAX_COLUMNS}；"
                           "请只导出契约里需要的字段后重试"]}
    if len(rows) > MAX_ROWS:
        return {"error": "too_many_rows",
                "errors": [f"共 {len(rows)} 行，超过单文件硬上限 {MAX_ROWS} 行；"
                           "请拆分为多个文件分批同步（不做隐式截断）"]}
    return {"format": fmt, "format_label": FORMAT_LABEL.get(fmt, fmt),
            "file_name": name, "size_bytes": len(raw), "sha256": sha256_bytes(raw),
            "columns": columns, "rows": rows, "row_count": len(rows),
            "batch_count": max(1, -(-len(rows) // BATCH_ROWS)),
            "batch_rows": BATCH_ROWS, "warnings": warnings}


def preview_rows(rows: list[dict], limit: int = 5) -> list[dict]:
    return [dict(r) for r in (rows or [])[:max(1, int(limit))]]
