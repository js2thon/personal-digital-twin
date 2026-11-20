"""P0.5：整表重算 semantic_chunks 的向量 + 本体坐标 + model/version。

必须在仓库根执行（Settings.env_file=".env" 相对 CWD，backend/ 下跑会误判网关不可达）：
    python backend/scripts/backfill_embeddings.py            # 整表重算（换模型/口径时用）
    python backend/scripts/backfill_embeddings.py --missing  # 只补 embedded_at IS NULL 的

设计要点：
  - UPDATE 现有行，不 DELETE/INSERT：保留 chunk id / created_at / 原 metadata；
  - 小批量 + 断点续跑：网关限流时单批失败只记数，不中断，重跑 --missing 续；
  - 整表重算而非"只补空的"：现存 50 条向量无 model/version，与当前口径不可比，
    必须整表重算，禁止新旧混写（方案 §3.2 / §6）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.db import session as db  # noqa: E402
from app.modules.embeddings.service import EMBEDDING_VERSION, annotate_chunk  # noqa: E402
from app.modules.llm.client import llm_client  # noqa: E402
from app.modules.retrieval import vector_store  # noqa: E402

BATCH = 32
EMBED_TEXT_LEN = 1500  # 与 embed_chunks 保持一致（P1 再评估长切片策略）


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--missing", action="store_true",
                    help="只处理 embedded_at IS NULL 的切片（断点续跑）")
    ap.add_argument("--batch", type=int, default=BATCH)
    args = ap.parse_args()

    vector_store.ensure_columns()
    where = ("WHERE c.embedded_at IS NULL OR c.embedding_model IS NULL "
             "OR c.embedding_version IS NULL") if args.missing else ""
    rows = db.fetch_all(
        "SELECT c.id, c.content, c.metadata FROM knowledge.semantic_chunks c "
        + where + " ORDER BY c.created_at"
    )
    print(f"target chunks = {len(rows)} (model={settings.embedding_model}, "
          f"version={EMBEDDING_VERSION}, batch={args.batch})")

    done = failed = 0
    for start in range(0, len(rows), args.batch):
        batch = rows[start:start + args.batch]
        try:
            vectors = llm_client.embed(
                [str(r.get("content") or "")[:EMBED_TEXT_LEN] for r in batch],
                timeout=40,
            )
            if len(vectors) != len(batch):
                raise RuntimeError(
                    f"维度不匹配：返回 {len(vectors)} 条，期望 {len(batch)} 条"
                )
        except Exception as exc:  # noqa: BLE001  单批失败不中断，可断点续跑
            failed += len(batch)
            print(f"  batch {start // args.batch + 1} FAILED: "
                  f"{type(exc).__name__}: {str(exc)[:120]}")
            continue

        params = []
        for r, vec in zip(batch, vectors):
            ann = annotate_chunk(str(r.get("content") or ""))
            params.append({
                "id": r["id"],
                "v": json.dumps(vec) if isinstance(vec, list) else None,
                "m": settings.embedding_model,
                "ver": EMBEDDING_VERSION,
                "eids": json.dumps(ann["entity_ids"]),
                "etypes": ann["entity_types"],
                "ov": ann["ontology_version"],
                "meta": json.dumps({
                    "entity_ids": ann["entity_ids"],
                    "entity_types": ann["entity_types"],
                    "ontology_version": ann["ontology_version"],
                    "embedding_model": settings.embedding_model,
                    "embedding_version": EMBEDDING_VERSION,
                }, ensure_ascii=False),
            })
        db.executemany(
            "UPDATE knowledge.semantic_chunks SET "
            "embedding_vec = CAST(:v AS text), embedding_model = :m, "
            "embedding_version = :ver, embedded_at = now(), "
            "entity_ids = CAST(:eids AS jsonb), entity_types = CAST(:etypes AS text[]), "
            "ontology_version = :ov, metadata = metadata || CAST(:meta AS jsonb) "
            "WHERE id = :id",
            params,
        )
        done += len(batch)
        print(f"  batch {start // args.batch + 1}: +{len(batch)} "
              f"({done}/{len(rows)})")

    stats = vector_store.get_vector_store().stats()
    print(f"DONE written={done} failed={failed}")
    print(f"stats={json.dumps(stats, ensure_ascii=False)}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
