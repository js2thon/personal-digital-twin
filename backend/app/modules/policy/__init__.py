"""SIL P1-4：核心表落 tenant_id（幂等 DDL + 启动接入）。

P0-3 已给 `knowledge.facts` 落了 tenant_id（契约列）；本模块把其余读路径
核心表补齐：
* `ontology.entities`      —— 图遍历节点 / 实体画像
* `ontology.relations`     —— 图遍历逐跳（`_edge_allowed` 消费）
* `knowledge.semantic_chunks` —— 向量/词法检索通道（向量检索前过滤）
* `memory.memory_items`    —— 记忆通道（keyword 检索入口过滤）

只加列 + 回填 NULL→'default'，不改任何既有列语义；本部署当前单租户，
真实多租户矩阵接入时不需要再改 DDL。
"""
from __future__ import annotations

_TARGETS = ("ontology.entities", "ontology.relations", "knowledge.semantic_chunks",
            "memory.memory_items")


def ensure_tenant_columns() -> dict:
    """幂等补列 + 回填；返回每张表的处理结果（自证用）。"""
    from app.db import session as db

    out: dict[str, dict] = {}
    for table in _TARGETS:
        try:
            db.execute(f"ALTER TABLE {table} "
                       "ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64)")
            db.execute(f"ALTER TABLE {table} "
                       "ALTER COLUMN tenant_id SET DEFAULT 'default'")
            db.execute(f"UPDATE {table} SET tenant_id = 'default' "
                       "WHERE tenant_id IS NULL")
            out[table] = {"ok": True, "default": "default"}
        except Exception as exc:  # noqa: BLE001  单表失败不阻断其余表
            out[table] = {"ok": False, "error": str(exc)[:200]}
    return out


def coverage() -> dict:
    """四张核心表的 tenant_id 覆盖率（评估 GAP-P0-3 同款口径，只读）。"""
    from app.db import session as db

    out = {}
    for table in ("knowledge.facts",) + _TARGETS:
        try:
            row = db.fetch_one(
                "SELECT COUNT(*) AS total, "
                "COUNT(tenant_id) AS stamped, "
                "COUNT(*) FILTER (WHERE tenant_id IS NOT NULL "
                "                 AND tenant_id <> 'default') AS foreign "
                f"FROM {table}")
            total = int((row or {}).get("total") or 0)
            stamped = int((row or {}).get("stamped") or 0)
            out[table] = {
                "total": total, "stamped": stamped,
                "coverage": round(stamped / total, 4) if total else 1.0,
                "foreign_rows": int((row or {}).get("foreign") or 0),
            }
        except Exception as exc:  # noqa: BLE001
            out[table] = {"error": str(exc)[:200]}
    return out
