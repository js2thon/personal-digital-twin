"""数据接入：MinIO 原始存储。"""
import hashlib
import io
import json
import re

_INVALID_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

from app.config import settings
from app.db import session as db

try:
    from minio import Minio
except ImportError:  # 本地无 minio 依赖时降级
    Minio = None

_client = None
_bucket_ready = False  # MinIO 不可用时置 False 并缓存，避免每次请求都挂起重试


def get_minio():
    global _client, _bucket_ready
    # L14：没有显式凭据时不尝试连接，避免用代码内默认口令访问对象存储
    if not (settings.minio_access_key and settings.minio_secret_key):
        return None
    if Minio is None or _bucket_ready is None:
        return None
    if _client is None:
        try:
            from urllib3 import PoolManager, Timeout

            _client = Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=False,
                # 关键：连接/读取超时 2 秒。MinIO 未启动时快速失败，
                # 而不是默认无超时导致整个请求挂起 30 秒以上。
                http_client=PoolManager(timeout=Timeout(connect=2.0, read=2.0)),
            )
        except Exception:  # noqa: BLE001
            _bucket_ready = None
            return None
    if not _bucket_ready:
        try:
            if not _client.bucket_exists(settings.minio_bucket):
                _client.make_bucket(settings.minio_bucket)
            _bucket_ready = True
        except Exception:  # noqa: BLE001
            _bucket_ready = None  # 本进程内永久禁用，后续请求直接降级
            return None
    return _client


def sanitize_text(text: str) -> str:
    """清除 PostgreSQL UTF8 不接受的控制字符 / 孤立代理项。"""
    return _INVALID_RE.sub("", text or "")


def content_hash(text: str) -> str:
    # 防御：个别来源文本含孤立代理项，直接 encode() 会抛 UnicodeEncodeError → HTTP 500
    return hashlib.sha256(sanitize_text(text).encode("utf-8", errors="replace")).hexdigest()


def ensure_source(source_type: str, source_name: str) -> str:
    """按类型取得（或创建）数据源记录，让每份文档都能追溯到真实来源。"""
    row = db.fetch_one(
        "SELECT id FROM source.data_sources WHERE source_type = :t AND source_name = :n LIMIT 1",
        {"t": source_type, "n": source_name},
    )
    if row:
        return str(row["id"])
    return str(db.insert_returning_id(
        "source.data_sources",
        {"source_type": source_type, "source_name": source_name,
         "connector_config": json.dumps({"provider": "dws"}, ensure_ascii=False)},
    ))


def store_document(source_id: str, title: str, text: str, mime_type: str = "text/plain",
                   metadata: dict | None = None) -> str:
    """创建文档并存储原文；按「同一数据源内」的外部 ID / 标题+哈希 / 哈希去重。

    去重口径不再全局按 content_hash 串台：不同来源的两份同名内容必须各自留痕，
    否则后导入的 external_ref/source_kind 会盖掉前一个，导致「已蒸馏」状态误判。
    MinIO 不可用时降级：原文内联存入 metadata._inline（仅本地演示用）。
    """
    # 统一清洗为 PostgreSQL UTF8 安全文本（DWS 导入链路同样受益）
    text = sanitize_text(text)
    chash = content_hash(text)
    meta = dict(metadata or {})
    external_ref = str(meta.get("external_ref") or "").strip()[:500] or None
    sid = source_id or None

    existing = None
    if external_ref:
        existing = db.fetch_one(
            "SELECT id, metadata FROM source.documents "
            "WHERE external_id = :e AND source_id IS NOT DISTINCT FROM CAST(:s AS uuid) LIMIT 1",
            {"e": external_ref, "s": sid},
        )
    if not existing:
        existing = db.fetch_one(
            "SELECT id, metadata FROM source.documents WHERE content_hash = :h "
            "AND title = :t AND source_id IS NOT DISTINCT FROM CAST(:s AS uuid) LIMIT 1",
            {"h": chash, "t": title, "s": sid},
        )
    if not existing:
        existing = db.fetch_one(
            "SELECT id, metadata FROM source.documents WHERE content_hash = :h "
            "AND source_id IS NOT DISTINCT FROM CAST(:s AS uuid) LIMIT 1",
            {"h": chash, "s": sid},
        )
    if existing:
        # 只补齐缺失键，绝不覆盖已有的来源标识（external_ref / source_kind）
        old = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
        merged = dict(old)
        for k, v in meta.items():
            if k not in merged or merged[k] in (None, "", {}):
                merged[k] = v
        db.execute(
            "UPDATE source.documents SET metadata = CAST(:m AS jsonb), "
            "external_id = COALESCE(external_id, :e) WHERE id = :id",
            {"m": json.dumps(merged, ensure_ascii=False, default=str),
             "e": external_ref, "id": str(existing["id"])},
        )
        return str(existing["id"])

    object_uri = None
    inline_meta: dict = dict(meta)
    try:
        mc = get_minio()
    except Exception:  # noqa: BLE001
        mc = None
    if mc is not None:
        try:
            object_name = f"docs/{chash}.txt"
            mc.put_object(
                settings.minio_bucket,
                object_name,
                io.BytesIO(text.encode()),
                length=len(text.encode()),
                content_type=mime_type,
            )
            object_uri = f"s3://{settings.minio_bucket}/{object_name}"
        except Exception:  # noqa: BLE001
            mc = None
    if mc is None:
        inline_meta["_inline"] = text[:200000]
        # 原文被静默截断必须留痕：蒸馏出的 Claim 缺后半部分时可在任务结果中提示
        if len(text) > 200000:
            inline_meta["_inline_truncated"] = True
            inline_meta["_inline_truncated_len"] = len(text)

    return str(
        db.insert_returning_id(
            "source.documents",
            {
                "source_id": sid,
                "external_id": external_ref,
                "title": title,
                "content_hash": chash,
                "object_uri": object_uri,
                "mime_type": mime_type,
                "metadata": json.dumps(inline_meta),
                "document_status": "pending",
            },
        )
    )


def get_document_text(document_id: str) -> str:
    row = db.fetch_one(
        "SELECT object_uri, metadata FROM source.documents WHERE id = :id",
        {"id": document_id},
    )
    if not row:
        return ""
    meta = row["metadata"] if isinstance(row["metadata"], dict) else {}
    if meta.get("_inline"):
        return meta["_inline"]
    if not row["object_uri"]:
        return ""
    try:
        mc = get_minio()
    except Exception:  # noqa: BLE001
        return ""
    if not mc:
        return ""
    bucket, object_name = row["object_uri"].removeprefix("s3://").split("/", 1)
    resp = mc.get_object(bucket, object_name)
    try:
        return resp.read().decode("utf-8", errors="replace")
    finally:
        resp.close()
        resp.release_conn()
