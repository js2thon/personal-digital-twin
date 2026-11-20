"""文件解析回归：表格 / Markdown / JSON 上传。

核心约束（与 docs/structured-ingestion-assessment.md 一致）：
* 文件只是「行的载体」，解析结果与湖仓取数同构，仍走同一套契约与断言；
* 不做列名语义猜测，也不静默丢弃（编码、多表格、嵌套值、超限都要显式暴露）；
* 超行数拒绝而非截断 —— 截断会让「同步成功」变成假象。
"""
import copy
import io
import json

import pytest

from app.modules.structured import assertions as az
from app.modules.structured import contracts as ct
from app.modules.structured import files as fl
from app.modules.structured import service as svc

HEADER = "employee_id,employee_name,dept_name,job_title,manager_id,manager_name,effective_from"


def csv_bytes(rows: str = "E1001,张三,数据管理部,负责人,,,2024-01-01") -> bytes:
    return f"{HEADER}\n{rows}\n".encode("utf-8")


# ---------------------------------------------------------------- 表格


def test_csv_basic():
    r = fl.parse_upload("org.csv", csv_bytes())
    assert r["format"] == "csv" and r["row_count"] == 1
    assert r["columns"] == HEADER.split(",")
    assert r["rows"][0]["dept_name"] == "数据管理部"
    assert r["warnings"] == []


def test_tsv_uses_tab_without_sniffing():
    raw = "employee_id\temployee_name\nE1\t王五\n".encode()
    r = fl.parse_upload("org.tsv", raw)
    assert r["columns"] == ["employee_id", "employee_name"]
    assert r["rows"][0]["employee_name"] == "王五"


def test_gbk_file_decoded():
    raw = "employee_id,employee_name\nE1,张伟\n".encode("gbk")
    r = fl.parse_upload("gbk.csv", raw)
    assert r["rows"][0]["employee_name"] == "张伟"


def test_blank_and_duplicate_headers_are_reported():
    raw = "employee_id,,dept_name,dept_name\nE1,x,数据管理部,数据管理部\n".encode("utf-8")
    r = fl.parse_upload("dup.csv", raw)
    assert r["columns"] == ["employee_id", "col2", "dept_name", "dept_name_2"]
    assert any("表头为空" in w for w in r["warnings"])
    assert any("重复" in w for w in r["warnings"])


def test_xlsx_reads_first_sheet_and_normalizes_numbers_and_dates():
    from datetime import date

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "花名册"
    ws.append(HEADER.split(","))
    ws.append(["E1002", "某某丙", "产品部", "分析师", 1001.0, "本人", date(2025, 7, 1)])
    bio = io.BytesIO()
    wb.save(bio)

    r = fl.parse_upload("org.xlsx", bio.getvalue())
    assert r["format"] == "xlsx" and r["row_count"] == 1
    # 1001.0 必须归一成 1001，否则工号会变成两个不同锚点
    assert r["rows"][0]["manager_id"] == "1001"
    assert r["rows"][0]["effective_from"] == "2025-07-01"


def test_xlsx_missing_sheet_falls_back_with_warning():
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(HEADER.split(","))
    wb.active.append(["E1", "张三", "数据管理部", "负责人", "", "", "2024-01-01"])
    bio = io.BytesIO()
    wb.save(bio)
    r = fl.parse_upload("org.xlsx", bio.getvalue(), sheet="不存在的表")
    assert r["row_count"] == 1
    assert any("不存在" in w for w in r["warnings"])


# ---------------------------------------------------------------- Markdown / JSON


MD = """# 组织关系

说明：下列第一张表是本次要同步的数据。

| employee_id | employee_name | dept_name |
| --- | --- | --- |
| E1001 | 张三 | 数据管理部 |
| E1002 | 李四 | 产品部 |
| E1003 | 王五 | 数据管理部 |

| 备注 | 说明 |
| --- | --- |
| a | b |
"""


def test_markdown_picks_largest_table_and_warns():
    r = fl.parse_upload("org.md", MD.encode())
    assert r["columns"] == ["employee_id", "employee_name", "dept_name"]
    assert r["row_count"] == 3
    assert any("检测到 2 个表格" in w for w in r["warnings"])


def test_markdown_without_separator_is_rejected():
    r = fl.parse_upload("bad.md", b"| a | b |\n| 1 | 2 |\n")
    assert r["error"] == "parse_failed"
    assert "Markdown 表格" in r["errors"][0]


def test_json_array_and_nested_value_warning():
    raw = ('[{"employee_id":"E1","dept":{"code":"D1"},"score":3.0},'
           '{"employee_id":"E2","dept":"产品部","score":4}]').encode()
    r = fl.parse_upload("org.json", raw)
    assert r["columns"] == ["employee_id", "dept", "score"]
    assert r["row_count"] == 2
    assert r["rows"][0]["dept"] == '{"code": "D1"}'   # 嵌套值不静默丢弃
    assert r["rows"][1]["dept"] == "产品部"
    assert any("嵌套结构" in w for w in r["warnings"])


def test_json_rows_wrapper_and_missing_keys_filled():
    raw = b'{"rows":[{"employee_id":"E1","dept_name":"A"},{"employee_id":"E2"}]}'
    r = fl.parse_upload("wrap.json", raw)
    assert r["row_count"] == 2
    assert r["rows"][1]["dept_name"] == ""


def test_json_non_object_items_ignored_with_warning():
    r = fl.parse_upload("mix.json", b'[{"employee_id":"E1"}, 42]')
    assert r["row_count"] == 1
    assert any("不是对象" in w for w in r["warnings"])


# ---------------------------------------------------------------- 上限与错误


def test_unsupported_extension():
    r = fl.parse_upload("org.txt", b"a,b\n1,2\n")
    assert r["error"] == "unsupported_format"


def test_empty_file():
    assert fl.parse_upload("org.csv", b"")["error"] == "empty_file"


def test_too_many_rows_rejected_not_truncated():
    body = "\n".join(f"E{i},员工{i},数据管理部" for i in range(fl.MAX_ROWS + 1))
    r = fl.parse_upload("big.csv", f"employee_id,employee_name,dept_name\n{body}\n".encode())
    assert r["error"] == "too_many_rows"
    assert str(fl.MAX_ROWS) in r["errors"][0]


def test_too_many_columns():
    cols = [f"c{i}" for i in range(fl.MAX_COLUMNS + 1)]
    raw = (",".join(cols) + "\n" + ",".join("x" for _ in cols) + "\n").encode()
    assert fl.parse_upload("wide.csv", raw)["error"] == "too_many_columns"


def test_wide_table_over_80_columns_is_accepted():
    """HR / 组织导出常见上百列：80 列曾把正常宽表整体拒掉，这里锁住不回归。"""
    cols = [f"c{i}" for i in range(120)]
    body = "\n".join(",".join(f"v{i}_{r}" for i in range(120)) for r in range(20))
    raw = (",".join(cols) + "\n" + body + "\n").encode()
    r = fl.parse_upload("wide.csv", raw)
    assert "error" not in r
    assert len(r["columns"]) == 120 and r["row_count"] == 20
    assert r["rows"][-1]["c119"] == "v119_19"


def test_json_wide_table_over_80_columns_is_accepted():
    item = {f"c{i}": f"v{i}" for i in range(100)}
    r = fl.parse_upload("wide.json", json.dumps([item, item]).encode())
    assert "error" not in r
    assert len(r["columns"]) == 100 and r["row_count"] == 2


def test_too_many_cells_rejected(monkeypatch):
    """宽表用「行 × 列」折算行数上限，保护进程内存且不静默截断。"""
    monkeypatch.setattr(fl, "MAX_CELLS", 1000)
    cols = [f"c{i}" for i in range(120)]
    body = "\n".join(",".join("x" for _ in cols) for _ in range(20))
    raw = (",".join(cols) + "\n" + body + "\n").encode()
    r = fl.parse_upload("wide.csv", raw)
    assert r["error"] == "too_many_cells"
    assert "单元格" in r["errors"][0]


def test_oversize_file_rejected_before_parsing(monkeypatch):
    # 不真的构造 100 MiB 数据：把上限调小即可验证「解析前就拒绝」的顺序
    monkeypatch.setattr(fl, "MAX_BYTES", 1024)
    r = fl.parse_upload("huge.csv", b"a" * 1025)
    assert r["error"] == "too_large"
    assert "1 MiB" in r["errors"][0] or "0 MiB" in r["errors"][0]


def test_upload_budget_is_100_mib_with_auto_batching():
    # 字节 100 MiB；5000 行是「自动分批」阈值；20 万行是保护进程内存的硬上限
    assert fl.MAX_BYTES == 100 * 1024 * 1024
    assert fl.BATCH_ROWS == 5000
    assert fl.MAX_ROWS == 200000
    # 列数只拦异常表头；真正的内存闸门是「行 × 列」的单元格总数
    assert fl.MAX_COLUMNS == 512
    assert fl.MAX_CELLS == 6_000_000


def test_parse_reports_batch_count(monkeypatch):
    monkeypatch.setattr(fl, "BATCH_ROWS", 2)
    body = "\n".join(f"E{i},员工{i},数据管理部,工程师,,,2025-01-01" for i in range(5))
    r = fl.parse_upload("org.csv", f"{HEADER}\n{body}\n".encode())
    assert r["row_count"] == 5
    assert r["batch_count"] == 3 and r["batch_rows"] == 2


def test_auto_batch_does_not_drop_rows(monkeypatch):
    """分批只是切批，绝不截断：断言数与行数必须逐行对上。"""
    monkeypatch.setattr(fl, "BATCH_ROWS", 3)
    body = "\n".join(f"E{i},员工{i},数据管理部,工程师,,,2025-01-01" for i in range(10))
    parsed = fl.parse_upload("org.csv", f"{HEADER}\n{body}\n".encode())
    out = _build(parsed["rows"], {"system": "file", "table": "org.csv"})
    assert parsed["batch_count"] == 4
    assert out["stats"]["rows"] == 10
    assert out["stats"]["assertions"] == 10


def test_row_limit_aborts_while_streaming(monkeypatch):
    # 上限命中后立刻中止：给一个「无限」CSV 迭代器也应快速返回，不会先建成整表
    monkeypatch.setattr(fl, "MAX_ROWS", 3)
    body = "\n".join(f"E{i},员工{i},数据管理部" for i in range(10))
    r = fl.parse_upload("s.csv", f"employee_id,employee_name,dept_name\n{body}\n".encode())
    assert r["error"] == "too_many_rows"
    assert "3 行" in r["errors"][0]


def test_xlsx_large_rows_still_bounded(monkeypatch):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["employee_id", "employee_name", "dept_name"])
    for i in range(6):
        ws.append([f"E{i}", f"员工{i}", "数据管理部"])
    buf = io.BytesIO()
    wb.save(buf)
    monkeypatch.setattr(fl, "MAX_ROWS", 5)
    r = fl.parse_upload("org.xlsx", buf.getvalue())
    assert r["error"] == "too_many_rows"


# ---------------------------------------------------------------- 与契约/断言的衔接


RAW = {
    "entity": {"entity_type": "Person", "pk_column": "employee_id",
               "name_column": "employee_name", "external_id_prefix": "hr:"},
    "fields": [
        {"column": "dept_name", "predicate": "belongs_to_department",
         "object_kind": "literal", "visibility": "organization",
         "valid_from_column": "effective_from", "required": True},
    ],
    "expect_columns": HEADER.split(","),
}


def _build(rows, source_ref):
    errors, clean = ct.validate_mapping(copy.deepcopy(RAW))
    assert not errors, errors
    return az.build_assertions(
        mapping=clean, rows=rows, contract_name="file_org", version=1,
        contract_id="00000000-0000-0000-0000-000000000021",
        contract_hash="f" * 64, source_ref=source_ref, batch_id="upload-test",
        synced_at="2026-11-12T00:00:00+00:00")


def test_parsed_csv_feeds_assertions_and_passes_schema_check():
    parsed = fl.parse_upload("org.csv", csv_bytes())
    out = _build(parsed["rows"], {"system": "file", "table": "org.csv"})
    assert out["schema_ok"] is True
    assert len(out["assertions"]) == 1
    assert out["assertions"][0]["predicate"] == "belongs_to_department"
    assert out["assertions"][0]["source_ref"]["table"] == "org.csv"


def test_extra_column_from_file_triggers_schema_drift():
    parsed = fl.parse_upload("org.csv", (HEADER + ",performance_score\n"
                                        "E1,张三,数据管理部,负责人,,,2024-01-01,3.75\n").encode())
    out = _build(parsed["rows"], {"system": "file", "table": "org.csv"})
    assert out["schema_ok"] is False
    assert out["anomalies"][0]["reason"] == "schema_drift"


def test_contract_source_keeps_file_fingerprint_and_falls_back_to_filename():
    contract = {"source_ref": {"system": "hr", "table": ""}}
    upload = {"id": "u-1", "file_name": "org.csv", "format": "csv", "sha256": "a" * 64}
    src = svc._contract_source(contract, upload)
    assert src["table"] == "org.csv"      # 空表名回填文件名，幂等键才稳定
    assert src["file_sha256"] == "a" * 64
    assert src["upload_id"] == "u-1"
    assert src["system"] == "hr"          # 契约显式声明的系统不被覆盖


def test_contract_source_without_upload_is_unchanged():
    contract = {"source_ref": {"system": "hr", "table": "dw_employee"}}
    assert svc._contract_source(contract, None) == {"system": "hr", "table": "dw_employee"}


def test_file_metadata_reaches_fact_source_ref():
    parsed = fl.parse_upload("org.csv", csv_bytes())
    out = _build(parsed["rows"], svc._contract_source(
        {"source_ref": {}}, {"id": "u-9", "file_name": "org.csv", "format": "csv",
                             "sha256": "b" * 64}))
    source = out["assertions"][0]["source_ref"]
    assert source["file_name"] == "org.csv"
    assert source["file_format"] == "csv"
    assert source["upload_id"] == "u-9"
    # 六要素仍然齐备：链路校验第 8 项不能因为文件接入而变红
    for key in ("table", "pk", "row_hash", "batch_id", "mapping_version",
                "idempotency_key"):
        assert source.get(key)


def test_formats_endpoint_metadata_matches_parser():
    from app.api.v1 import structured as api

    meta = api.formats()
    assert set(meta["supported"]) == set(fl.SUPPORTED)
    assert meta["max_rows"] == fl.MAX_ROWS
    assert meta["max_bytes"] == fl.MAX_BYTES


@pytest.mark.parametrize("fmt", list(fl.SUPPORTED))
def test_every_supported_format_has_label(fmt):
    assert fl.FORMAT_LABEL.get(fmt)


# ---------------------------------------------------------------- 上传接口限流读取


def test_read_limited_passes_when_under_limit():
    import asyncio

    from starlette.datastructures import UploadFile

    from app.api.v1 import structured as api

    f = UploadFile(filename="a.csv", file=io.BytesIO(b"x" * 3000))
    assert asyncio.run(api._read_limited(f, 4096)) == b"x" * 3000


def test_read_limited_raises_413_on_overflow():
    import asyncio

    from fastapi import HTTPException
    from starlette.datastructures import UploadFile

    from app.api.v1 import structured as api

    f = UploadFile(filename="a.csv", file=io.BytesIO(b"x" * 5000))
    with pytest.raises(HTTPException) as e:
        asyncio.run(api._read_limited(f, 4096))
    assert e.value.status_code == 413
