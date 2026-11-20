"""钉钉文档（DWS CLI）集成：列出我的钉钉文档、读取内容用于知识蒸馏。

依赖本机已安装并登录 dws CLI（https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli）。
dws 未安装 / 未登录时所有函数优雅降级返回空或抛出 DwsError。
"""
import json
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

DWS_TIMEOUT_SEARCH = 60
DWS_TIMEOUT_READ = 180
DWS_TIMEOUT_EXPORT = 330


class DwsError(Exception):
    pass


def _dws_bin() -> str:
    path = shutil.which("dws")
    if not path:
        raise DwsError("dws CLI 未安装（https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli）")
    return path


def _parse_json_loose(text: str) -> dict | None:
    """解析 dws 输出：可能混入 [WARN] 前缀，且错误 JSON 可能只在 stderr。"""
    idx = text.find("{")
    if idx < 0:
        return None
    try:
        return json.loads(text[idx:])
    except json.JSONDecodeError:
        return None


def _check_dws_error(data: dict) -> None:
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        if isinstance(err, dict):
            raise DwsError(err.get("message") or err.get("hint") or "dws 业务错误")
        raise DwsError(str(err))


def _run(args: list[str], timeout: int) -> dict:
    """执行 dws 命令并解析 JSON 输出。"""
    try:
        proc = subprocess.run(
            [_dws_bin(), *args],
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise DwsError("dws 命令超时，请稍后重试")
    except OSError as e:
        raise DwsError(f"dws 执行失败: {e}")
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    data = _parse_json_loose(out) or _parse_json_loose(err)
    if data is None:
        raise DwsError(f"dws 输出解析失败: {(out or err)[:200]}")
    _check_dws_error(data)
    return data


_auth_cache: dict = {"ts": 0.0, "data": None}


def _is_authenticated() -> dict:
    """缓存登录状态 5 分钟，避免翻页时每次都多跑一次 dws auth status。"""
    now = time.time()
    if _auth_cache["data"] is not None and now - _auth_cache["ts"] < 300:
        return _auth_cache["data"]
    try:
        data = _run(["auth", "status"], 30)
    except DwsError:
        return {"authenticated": False}
    result = {
        "authenticated": bool(data.get("authenticated")),
        "user_name": data.get("user_name"),
        "corp_name": data.get("corp_name"),
    }
    _auth_cache.update(ts=now, data=result)
    return result


def _search_docs(query: str, limit: int, cursor: str | None = None) -> tuple[list, bool, str | None]:
    """搜索一页文档，返回 (docs, has_more, next_cursor)。

    首页用 CLI 推荐的 `drive search`（聚合钉盘 + 文档空间）；
    但聚合搜索明确规定不支持续页，续页须改用 `doc search --cursor`，
    两者首页结果一致（实测 30/30 重叠），可无缝衔接。
    """
    if cursor:
        data = _run(
            ["doc", "search", "--query", query or " ", "--limit", str(limit),
             "--cursor", cursor, "-f", "json"],
            DWS_TIMEOUT_SEARCH,
        )
        return (data.get("documents") or [], bool(data.get("hasMore")),
                data.get("nextPageToken"))
    data = _run(
        ["drive", "search", "--query", query or " ", "--limit", str(limit), "-f", "json"],
        DWS_TIMEOUT_SEARCH,
    )
    r = data.get("doc_results") or {}
    docs = r.get("documents") or []
    if not docs and isinstance(data.get("documents"), list):
        docs, r = data["documents"], data
    return docs, bool(r.get("hasMore")), r.get("nextPageToken")


def _search_files(query: str, limit: int, cursor: str | None = None) -> tuple[list, bool, str | None]:
    """钉盘文件侧独立分页链（--target file），用于补全聚合首页之外的文件。

    空关键词时 CLI 会报「缺少必要参数 keyword」，此处静默跳过。
    """
    args = ["drive", "search", "--query", query, "--limit", str(limit),
            "--target", "file", "-f", "json"]
    if cursor:
        args += ["--cursor", cursor]
    try:
        data = _run(args, DWS_TIMEOUT_SEARCH)
    except DwsError:
        return [], False, None
    if data.get("error"):
        return [], False, None
    docs = []
    for x in data.get("items") or []:
        docs.append({
            "nodeId": x.get("fileId") or x.get("nodeId"),
            "name": x.get("name"),
            "extension": x.get("extension"),
            "docUrl": x.get("docUrl"),
            "updateTime": x.get("modifyTime") or x.get("createTime"),
            "nodeType": "folder" if x.get("type") == "folder" else None,
        })
    return docs, bool(data.get("hasMore")), data.get("nextCursor")


def _to_items(docs: list) -> list:
    """把 dws 原始文档元数据归一化成控制台所需结构。"""
    NON_TEXT_EXT = {"folder", "dlink", "shortcut", "", "mp4", "mp3", "avi",
                    "mov", "wav", "m4a", "jpg", "jpeg", "png", "gif", "webp"}
    items = []
    for x in docs or []:
        if x.get("nodeType") == "folder":
            continue
        ext = (x.get("extension") or "").lower()
        update_ts = x.get("updateTime") or x.get("lastEditTime") or x.get("createTime")
        readable = ext not in NON_TEXT_EXT
        if ext in ("adoc", "alidoc"):
            engine = "dws doc read"
        elif ext in ("axls", "able"):
            engine = "dws sheet read"
        elif ext == "pdf":
            engine = "MinerU / opendataloader-pdf / docling / pypdf"
        elif ext in ("docx", "xlsx", "pptx", "doc", "xls", "ppt"):
            engine = "docling / Office 解析器"
        elif ext in ("md", "txt", "csv"):
            engine = "内置文本读取"
        else:
            engine = "dws 下载 + 通用解析"
        items.append({
            "node_id": x.get("nodeId"),
            "name": x.get("name"),
            "extension": ext,
            "doc_url": x.get("docUrl"),
            "readable": readable,
            "engine": engine,
            "update_time": (
                datetime.fromtimestamp(update_ts / 1000, tz=timezone.utc)
                .astimezone().strftime("%Y-%m-%d %H:%M")
                if update_ts else None
            ),
        })
    return items


def _collect_all(query: str, limit: int, max_pages: int) -> tuple[list, int, bool]:
    """顺序翻页抓取当前账号有权限访问的全部文档，返回 (docs, pages, truncated)。

    dws 的 hasMore 在部分账号会一直为 True 且后期页面回绕重复，
    因此用「连续 2 页无新增」+ max_pages 双重终止条件，避免死循环。
    """
    seen: set = set()
    all_docs: list = []
    pages = 0
    truncated = False

    def add(docs: list) -> int:
        new = 0
        for d in docs:
            nid = d.get("nodeId")
            if nid and nid not in seen:
                seen.add(nid)
                all_docs.append(d)
                new += 1
        return new

    # 文档链（在线文档 + 最近访问）
    cursor, stagnant = None, 0
    for i in range(max_pages):
        docs, has_more, nxt = _search_docs(query, limit, cursor)
        pages += 1
        new = add(docs)
        stagnant = stagnant + 1 if new == 0 else 0
        if not has_more or not nxt or stagnant >= 2:
            break
        cursor = nxt
        if i == max_pages - 1:
            truncated = True

    # 钉盘文件链（仅关键词搜索有效，补齐聚合首页之外的普通文件）。
    # 该链通常 3~4 页即结束，单独限制预算，避免关键词全量时耗时失控。
    if query and query.strip():
        file_budget = min(max_pages, 4)
        fcur, fstagnant = None, 0
        for i in range(file_budget):
            fdocs, fhas, fnxt = _search_files(query, limit, fcur)
            pages += 1
            new = add(fdocs)
            fstagnant = fstagnant + 1 if new == 0 else 0
            if not fhas or not fnxt or fstagnant >= 2:
                break
            fcur = fnxt
            if i == file_budget - 1 and fhas:
                truncated = True
    return all_docs, pages, truncated


def list_documents(query: str = " ", limit: int = 30, cursor: str | None = None,
                   all_pages: bool = False, max_pages: int = 20) -> dict:
    """列出钉钉文档。

    单页模式（默认）：返回一页 + has_more/next_cursor，供控制台翻页。
    全量模式（all_pages=True）：自动顺序翻页抓取有权限的全部文档并去重，
    合并文档链与钉盘文件链；max_pages 为安全上限（默认 20 页 ≈ 600 条）。

    注意：dws API 每页 pageSize 限制 1~30，超限直接报业务错误。
    """
    limit = max(1, min(int(limit), 30))
    max_pages = max(1, min(int(max_pages), 50))
    if all_pages:
        docs, pages, truncated = _collect_all(query or " ", limit, max_pages)
        items = _to_items(docs)
        return {"items": items, "auth": _is_authenticated(),
                "engines": _parser_engines(), "all": True,
                "count": len(items), "total": len(items),
                "has_more": truncated, "next_cursor": None,
                "pages_fetched": pages, "truncated": truncated, "limit": limit}
    docs, has_more, next_cursor = _search_docs(query or " ", limit, cursor)
    items = _to_items(docs)
    return {"items": items, "auth": _is_authenticated(),
            "engines": _parser_engines(), "all": False,
            "count": len(items), "has_more": has_more,
            "next_cursor": next_cursor if has_more else None,
            "cursor": cursor, "limit": limit}


def _parser_engines() -> dict:
    from app.modules.document_parser import adapters

    return adapters.available_engines()


def read_document(node_id: str) -> str:
    """读取在线文档(adoc)内容，返回 Markdown 文本。"""
    data = _run(["doc", "read", "--node-id", node_id], DWS_TIMEOUT_READ)
    if isinstance(data, dict) and data.get("error"):
        raise DwsError(data["error"].get("message", "文档读取失败"))
    md = data.get("markdown") if isinstance(data, dict) else None
    if not md:
        raise DwsError("文档内容为空或非在线文档(adoc)")
    return md


def _col_letters(n: int) -> str:
    """1 → A、26 → Z、27 → AA。"""
    s = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s or "A"


def _sheet_range(sh: dict) -> tuple[str, bool]:
    """按工作表元数据算出实际读取区域；缺失元数据时退化为默认上限。

    返回 (A1 区域, 是否发生截断)。dws 常见字段名 rowCount/columnCount、
    rows/cols、row_count/column_count 都兼容。
    """
    max_rows = max(1, min(int(settings.dws_sheet_max_rows), 10000))
    max_cols = max(1, min(int(settings.dws_sheet_max_cols), 200))
    def pick(*keys: str) -> int:
        for k in keys:
            v = sh.get(k)
            if isinstance(v, int) and v > 0:
                return v
            if isinstance(v, str) and v.isdigit() and int(v) > 0:
                return int(v)
        return 0
    rows = pick("rowCount", "row_count", "rows", "totalRows") or max_rows
    cols = pick("columnCount", "column_count", "cols", "columns",
                "totalColumns") or max_cols
    truncated = rows > max_rows or cols > max_cols
    return f"A1:{_col_letters(min(cols, max_cols))}{min(rows, max_rows)}", truncated


def read_sheet(node_id: str) -> str:
    """读取钉钉在线表格(axls)：列出工作表并逐表按实际尺寸读取，转成 Markdown 表格文本。"""
    sheets = _run(["sheet", "list", "--node", node_id], DWS_TIMEOUT_READ)
    items = sheets.get("sheets") or sheets.get("worksheets") or []
    if isinstance(sheets, dict) and sheets.get("result"):
        items = sheets["result"].get("sheets") or items
    if not items:
        raise DwsError("未读取到工作表")
    parts = []
    for sh in items:
        sid = sh.get("sheetId") or sh.get("id") or sh.get("sheet_id")
        name = sh.get("name") or sh.get("title") or "工作表"
        if not sid:
            continue
        rng, truncated = _sheet_range(sh)
        try:
            data = _run(["sheet", "range", "read", "--node", node_id,
                         "--sheet-id", str(sid), "--range", rng], DWS_TIMEOUT_READ)
            # dws 返回 cells 二维数组，每个 cell 为 {"value": ...}
            cells = data.get("cells") or data.get("values") or (
                (data.get("result") or {}).get("cells") or [])
            rows = []
            for row in cells:
                vals = []
                for cell in row:
                    if isinstance(cell, dict):
                        v = cell.get("value")
                    else:
                        v = cell
                    vals.append("" if v is None else str(v))
                if any(v.strip() for v in vals):
                    rows.append(" | ".join(vals))
            if rows:
                note = (f"\n\n> 注：本工作表超出读取上限，仅读取 {rng}"
                        if truncated else "")
                parts.append(f"## 工作表：{name}\n\n" + "\n".join(rows) + note)
        except DwsError:
            continue
    if not parts:
        raise DwsError("表格无可读取内容")
    return "\n\n".join(parts)


def download_file(node_id: str, filename: str, ext: str = "") -> tuple[str, str]:
    """下载钉盘普通文件到本地临时目录，返回 (本地路径, 实际文件名)。

    钉钉返回的 name 常不带扩展名，这里按文档 extension 补全，
    否则解析器会把 Office/PDF 二进制误判为纯文本。
    """
    td = tempfile.mkdtemp(prefix="twin_dws_")
    base = Path(filename or "download.bin").name
    suffix = ext if ext.startswith(".") else ("." + ext if ext else "")
    if suffix and not base.lower().endswith(suffix.lower()):
        base = base + suffix
    out = Path(td) / base
    proc = subprocess.run(
        [_dws_bin(), "drive", "download", "--node", node_id, "--output", str(out)],
        capture_output=True, timeout=DWS_TIMEOUT_EXPORT,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not out.exists():
        raise DwsError(f"下载失败: {(proc.stderr or proc.stdout or '')[:200]}")
    return str(out), out.name


def export_document(node_id: str, name: str) -> str:
    """把在线文档导出为 Markdown（doc export），返回文本内容。"""
    import shutil

    td = tempfile.mkdtemp(prefix="twin_dws_export_")
    try:
        out = Path(td) / "export.md"
        proc = subprocess.run(
            [_dws_bin(), "doc", "export", "--node", node_id,
             "--export-format", "markdown", "--output", str(out)],
            capture_output=True, timeout=DWS_TIMEOUT_EXPORT,
            encoding="utf-8", errors="replace",
        )
        mds = list(Path(td).rglob("*.md"))
        if mds:
            return mds[0].read_text(encoding="utf-8", errors="replace")
        raise DwsError(f"导出失败: {(proc.stderr or proc.stdout or '')[:200]}")
    finally:
        # 文本读入内存后临时目录即无用，长期运行不能留垃圾
        shutil.rmtree(td, ignore_errors=True)


def fetch_document_text(node_id: str, extension: str, name: str) -> dict:
    """按钉钉文档类型路由，统一返回 {text, engine, warning}。

    路由：
      adoc / 在线文档 → dws doc read（Markdown）
      axls / able 钉钉表格 → dws sheet range read
      普通文件(docx/xlsx/pptx/pdf/...) → dws drive download → 解析适配层
    """
    from app.modules.document_parser import adapters

    ext = (extension or "").lower()
    if ext in ("adoc", "alidoc", ""):
        try:
            text = read_document(node_id)
        except DwsError as e:
            # 在线文档读失败时尝试导出 Markdown
            try:
                text = export_document(node_id, name)
            except DwsError:
                raise e
        return {"text": adapters.clean_text(text), "engine": "dws:doc-read", "warning": None}
    if ext in ("axls", "able"):
        return {"text": adapters.clean_text(read_sheet(node_id)),
                "engine": "dws:sheet-read", "warning": None}
    if ext in ("folder", "dlink", "shortcut"):
        raise DwsError(f"{name or '该节点'}是文件夹/链接，无法蒸馏")

    path, fname = download_file(node_id, name or f"file{ext}", ext)
    try:
        parse_ext = Path(fname).suffix.lower() or ("." + ext if ext else "")
        result = adapters.parse_file(path, parse_ext)
        if not result.get("text"):
            raise DwsError(f"{name or ext} 解析失败：{result.get('warning') or '无文本输出'}")
        result["engine"] = "dws:download + " + result.get("engine", "?")
        return result
    finally:
        # 解析完成后清理下载临时目录（文本已在内存）
        import shutil

        shutil.rmtree(Path(path).parent, ignore_errors=True)
