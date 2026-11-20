"""文档解析适配层：把各类钉钉文档/附件统一转成可蒸馏的纯文本（Markdown）。

集成三种可插拔引擎，按可用性自动选择：
  1. MinerU      —— PDF/扫描件高精度解析（CLI: mineru / magic-pdf），适合复杂版式
  2. opendataloader-pdf —— PDF → Markdown/JSON（Java CLI 的 Python 封装），适合数字 PDF
  3. docling     —— PDF/DOCX/XLSX/PPTX/HTML/图片统一解析（IBM），覆盖 Office 全家桶

内置兜底（无需额外依赖）：python-docx / openpyxl / python-pptx / pypdf 及纯文本读取。
任一引擎缺失或失败时自动降级，绝不阻塞蒸馏流水线。
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".html", ".htm"}
# .axls 为钉钉表格导出扩展名，内容仍是 xlsx 格式
OFFICE_EXT = {".docx", ".xlsx", ".xlsm", ".axls", ".pptx", ".doc", ".xls", ".ppt"}
PDF_EXT = {".pdf"}

# PostgreSQL UTF8 不接受 NUL 与孤立代理项；Office/PDF 解析文本常含控制字符
_INVALID_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]"
)


def clean_text(text: str) -> str:
    """清洗为 PostgreSQL UTF8 可安全存储的文本。"""
    return _INVALID_RE.sub("", text or "")


def available_engines() -> dict:
    """返回各解析引擎的可用性（供控制台/健康检查展示）。"""
    return {
        "mineru": bool(shutil.which("mineru") or shutil.which("magic-pdf")),
        "opendataloader_pdf": _module_ok("opendataloader_pdf") and bool(shutil.which("java")),
        "docling": _module_ok("docling"),
        "builtin": True,
    }


def _module_ok(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def parse_bytes(data: bytes, filename: str, fast: bool = False) -> dict:
    """把文件字节解析为文本。返回 {text, engine, warning}。

    fast=True 时先用内置库（python-docx / openpyxl / python-pptx / pypdf）取正文，
    只有取到的文本太短才退回 docling / MinerU 这类重型引擎。需求文档蒸馏只关心
    「正文写了什么」，不值得为一个 30 页 docx 等一分钟的版式模型。

    注意：部分解析库（openpyxl read_only 等）可能短暂持有文件句柄，
    Windows 下 TemporaryDirectory 退出时的强制删除会抛 WinError 32，
    把整个上传请求变成 HTTP 500。这里清洗文件名并手动容错清理，
    保证临时文件删除失败不会中断蒸馏。
    """
    ext = Path(filename or "").suffix.lower()
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_",
                       Path(filename or "file").name or "file") or "file"
    td = tempfile.mkdtemp(prefix="twin_parse_")
    try:
        path = Path(td) / safe_name
        path.write_bytes(data)
        return parse_file(str(path), ext, fast=fast)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def parse_file(path: str, ext: str | None = None, fast: bool = False) -> dict:
    """解析入口：包装 _parse_file_inner，统一清洗文本并限制大小。"""
    out = _parse_file_inner(path, ext, fast=fast)
    if out.get("text"):
        out["text"] = clean_text(out["text"])[:500000]
    return out


def _parse_file_inner(path: str, ext: str | None = None, fast: bool = False) -> dict:
    ext = (ext or Path(path).suffix).lower()
    engines = available_engines()

    # 纯文本直接读
    if ext in TEXT_EXT:
        try:
            return {"text": Path(path).read_text(encoding="utf-8", errors="replace"),
                    "engine": "builtin-text", "warning": None}
        except OSError as e:
            return {"text": "", "engine": "builtin-text", "warning": str(e)}

    # PDF：优先 MinerU → opendataloader-pdf → docling → pypdf
    # fast：先 pypdf 抽文字层，抽不到再按上面的重顺序降级
    if ext in PDF_EXT:
        order = (_parse_pypdf, _parse_mineru, _parse_opendataloader, _parse_docling) if fast \
            else (_parse_mineru, _parse_opendataloader, _parse_docling, _parse_pypdf)
        for fn in order:
            out = fn(path)
            if out.get("text"):
                return out
        return {"text": "", "engine": "none", "warning": "PDF 全部解析引擎不可用"}

    # Office：docling 优先（统一结构化），再按类型用内置库
    if ext in OFFICE_EXT:
        if engines["docling"] and not fast:
            out = _parse_docling(path)
            if out.get("text"):
                return out
        for fn in (_parse_docx, _parse_xlsx, _parse_pptx):
            out = fn(path, ext)
            if out.get("text"):
                return out
        # fast 模式内置库读不出东西（整页图片 / 复杂表格）时，仍然值得上一次 docling
        if fast and engines["docling"]:
            out = _parse_docling(path)
            if out.get("text"):
                return out
        return {"text": "", "engine": "none",
                "warning": f"暂不支持解析 {ext}（可安装 docling 后支持）"}

    # 其他类型兜底按文本尝试；二进制文件（高比例不可打印字节）直接拒绝
    try:
        raw = Path(path).read_bytes()
        if b"\x00" in raw[:8192]:
            return {"text": "", "engine": "none",
                    "warning": "二进制文件，缺少可用解析引擎或扩展名未识别"}
        text = raw.decode("utf-8", errors="replace")
        printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\r\t")
        if text and printable / len(text) < 0.85:
            return {"text": "", "engine": "none",
                    "warning": "检测到非文本内容，已跳过"}
        return {"text": text, "engine": "builtin-text-fallback",
                "warning": "未知扩展名，按纯文本处理"}
    except OSError as e:
        return {"text": "", "engine": "none", "warning": str(e)}


# ---------- 引擎实现 ----------
def _parse_mineru(path: str) -> dict:
    exe = shutil.which("mineru") or shutil.which("magic-pdf")
    if not exe:
        return {"text": "", "engine": "mineru", "warning": "not installed"}
    out_dir = tempfile.mkdtemp(prefix="twin_mineru_")
    try:
        subprocess.run([exe, "-p", path, "-o", out_dir], timeout=300,
                       capture_output=True, encoding="utf-8", errors="replace")
        mds = list(Path(out_dir).rglob("*.md"))
        text = "\n\n".join(m.read_text(encoding="utf-8", errors="replace") for m in mds)
        return {"text": text, "engine": "mineru", "warning": None}
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        return {"text": "", "engine": "mineru", "warning": str(e)[:200]}
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _parse_opendataloader(path: str) -> dict:
    if not available_engines()["opendataloader_pdf"]:
        return {"text": "", "engine": "opendataloader-pdf", "warning": "not available"}
    out_dir = tempfile.mkdtemp(prefix="twin_odl_")
    try:
        import opendataloader_pdf

        opendataloader_pdf.convert(input_path=[path], output_dir=out_dir,
                                   format="markdown")
        mds = list(Path(out_dir).rglob("*.md"))
        text = "\n\n".join(m.read_text(encoding="utf-8", errors="replace") for m in mds)
        return {"text": text, "engine": "opendataloader-pdf", "warning": None}
    except Exception as e:  # noqa: BLE001 - Java/依赖异常一律降级
        return {"text": "", "engine": "opendataloader-pdf", "warning": str(e)[:200]}
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _parse_docling(path: str) -> dict:
    if not available_engines()["docling"]:
        return {"text": "", "engine": "docling", "warning": "not installed"}
    try:
        from docling.document_converter import DocumentConverter

        doc = DocumentConverter().convert(path).document
        return {"text": doc.export_to_markdown(), "engine": "docling", "warning": None}
    except Exception as e:  # noqa: BLE001
        return {"text": "", "engine": "docling", "warning": str(e)[:200]}


def _parse_pypdf(path: str) -> dict:
    try:
        from pypdf import PdfReader

        # 用显式文件对象，确保读取后句柄立即释放（Windows 文件锁）
        with open(path, "rb") as fp:
            reader = PdfReader(fp)
            text = "\n\n".join((p.extract_text() or "") for p in reader.pages)
        return {"text": text, "engine": "pypdf", "warning": None}
    except Exception as e:  # noqa: BLE001
        return {"text": "", "engine": "pypdf", "warning": str(e)[:200]}


def _parse_docx(path: str, ext: str) -> dict:
    if ext != ".docx":
        return {"text": ""}
    try:
        from docx import Document

        with open(path, "rb") as fp:
            d = Document(fp)
            parts = [p.text for p in d.paragraphs]
            for t in d.tables:
                parts.append("\n".join(" | ".join(c.text for c in row.cells)
                                       for row in t.rows))
        return {"text": "\n\n".join(parts), "engine": "python-docx", "warning": None}
    except Exception as e:  # noqa: BLE001
        return {"text": "", "engine": "python-docx", "warning": str(e)[:200]}


def _parse_xlsx(path: str, ext: str) -> dict:
    if ext not in (".xlsx", ".xlsm", ".axls"):
        return {"text": ""}
    try:
        import openpyxl

        # 1) 传文件对象而非路径：openpyxl 会按扩展名白名单校验，
        #    .axls（钉钉表格）会被误拒；file-like 分支跳过该校验。
        # 2) read_only 模式会持续占用文件句柄，必须显式 close，
        #    否则 Windows 上临时文件删不掉 → 上传请求 500。
        with open(path, "rb") as fp:
            wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
            try:
                parts = []
                for ws in wb.worksheets:
                    parts.append(f"## 工作表：{ws.title}")
                    for row in ws.iter_rows(values_only=True):
                        cells = [str(c) for c in row if c is not None]
                        if cells:
                            parts.append(" | ".join(cells))
                return {"text": "\n".join(parts), "engine": "openpyxl", "warning": None}
            finally:
                wb.close()
    except Exception as e:  # noqa: BLE001
        return {"text": "", "engine": "openpyxl", "warning": str(e)[:200]}


def _parse_pptx(path: str, ext: str) -> dict:
    if ext != ".pptx":
        return {"text": ""}
    try:
        from pptx import Presentation

        with open(path, "rb") as fp:
            prs = Presentation(fp)
            parts = []
            for i, slide in enumerate(prs.slides, 1):
                parts.append(f"## 幻灯片 {i}")
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        parts.append(shape.text_frame.text)
        return {"text": "\n\n".join(parts), "engine": "python-pptx", "warning": None}
    except Exception as e:  # noqa: BLE001
        return {"text": "", "engine": "python-pptx", "warning": str(e)[:200]}
