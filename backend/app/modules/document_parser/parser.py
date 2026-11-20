"""文档解析与切片模块。"""
import re


def normalize_text(text: str) -> str:
    return re.sub(r"\r\n?", "\n", text).strip()


def chunk_text(
    text: str,
    max_chars: int = 800,
    overlap: int = 100,
) -> list[dict]:
    """基于段落的滑动窗口切片，返回 [{content, locator}]。

    locator 记录字符区间，作为 evidence_spans.locator 的一部分，
    保证每条知识可追溯回原文位置。
    """
    text = normalize_text(text)
    chunks: list[dict] = []
    paragraphs = text.split("\n\n")
    buf, start = "", 0

    def flush(buf_text: str, s: int):
        if buf_text.strip():
            chunks.append(
                {
                    "content": buf_text.strip(),
                    "locator": {"char_start": s, "char_end": s + len(buf_text)},
                }
            )

    for para in paragraphs:
        if len(buf) + len(para) <= max_chars:
            buf += ("\n\n" if buf else "") + para
        else:
            flush(buf, start)
            # 滑动重叠
            start = start + max(len(buf) - overlap, 0)
            tail = buf[-overlap:] if buf else ""
            buf = tail + "\n\n" + para
            if len(buf) > max_chars * 2:
                flush(buf, start)
                start = start + len(buf)
                buf = ""
    flush(buf, start)
    return chunks


def parse_plain(text: str) -> str:
    """MVP 解析器：纯文本/markdown/csv。PDF/Office 由 RAGFlow 处理。"""
    return normalize_text(text)
