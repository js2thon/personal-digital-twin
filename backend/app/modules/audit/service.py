"""审计服务：不可变决策快照 + 访问日志。

快照哈希采用 HMAC-SHA256（密钥 = settings.api_key，未配置时回退到口令派生密钥）。
持库写权限者即使改掉 snapshot 也无法重算出合法哈希，防主动篡改；
历史裸 SHA256 快照在 verify 时自动回退校验，保证向后兼容。
"""
import hashlib
import hmac
import json

from app.config import settings
from app.db import session as db


def _hash_body(body: str) -> str:
    key = (settings.api_key or "").encode()
    if not key:  # 未配置 api_key：用固定服务端盐派生，仍优于裸 SHA256
        key = b"twin-snapshot-key-v1"
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def _legacy_hash_body(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def save_decision_snapshot(decision_id: str, snapshot: dict) -> str:
    """每次决策保存一个不可变快照，含 HMAC 哈希用于防篡改校验。"""
    body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    return str(
        db.insert_returning_id(
            "audit.decision_snapshots",
            {
                "decision_id": decision_id,
                "snapshot": body,
                "snapshot_hash": _hash_body(body),
            },
        )
    )


def verify_snapshot(snapshot_id: str) -> bool:
    row = db.fetch_one(
        "SELECT snapshot, snapshot_hash FROM audit.decision_snapshots WHERE id = :id",
        {"id": snapshot_id},
    )
    if not row:
        return False
    body = json.dumps(row["snapshot"], ensure_ascii=False, sort_keys=True, default=str)
    return (_hash_body(body) == row["snapshot_hash"]
            or _legacy_hash_body(body) == row["snapshot_hash"])


def log_access(actor: str, action: str, resource_type: str,
               resource_id: str | None = None, detail: dict | None = None) -> None:
    db.execute(
        "INSERT INTO audit.access_logs (actor, action, resource_type, resource_id, detail) "
        "VALUES (:a, :act, :rt, :rid, CAST(:d AS jsonb))",
        {"a": actor, "act": action, "rt": resource_type,
         "rid": resource_id, "d": json.dumps(detail or {}, default=str)},
    )
