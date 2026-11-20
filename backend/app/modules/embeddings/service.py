"""Embedding 写入统一入口（向量本体 P0 · L1/L2）。

一处实现、两处调用：
  - workers.pipeline_tasks._persist_chunks   （首次蒸馏落库）
  - api.v1.knowledge.rechunk_documents       （历史文档重建切片）

消除此前"两套 INSERT"的漂移：rechunk 曾完全不写向量、不写本体坐标，
导致重建后的切片必然无向量。现在两处共用本函数，统一写：

  embedding_vec + embedding_model + embedding_version + embedded_at
  + entity_ids / entity_types / ontology_version（本体坐标）

派生数据原则：本体标注或 embedding 失败一律静默降级（保留原文与词法检索），
不抛异常、不回滚主事务。权威数据仍是原文与 facts。
"""
from __future__ import annotations

import json

from app.config import settings
from app.db import session as db
from app.modules.llm.client import llm_client

# 向量生成口径版本：截断长度、归一化方式、模型默认参数任一变化即升版。
# 换版本必须整表重算，禁止新旧口径混写（见方案 §6 风险表）。
EMBEDDING_VERSION = "v1"


def annotate_chunk(content: str, mentions: list[dict] | None = None) -> dict:
    """复用 detect_mentions 的别名索引给切片打本体坐标。

    标注失败不阻断：entity_ids=[]、entity_types=[]，切片仍可被词法/向量召回。
    """
    from app.modules.retrieval.service import detect_mentions

    if mentions is None:
        try:
            mentions = detect_mentions(content)
        except Exception:  # noqa: BLE001  本体表未就绪时退化为无坐标
            mentions = []
    entity_ids: list[str] = []
    entity_types: list[str] = []
    for m in mentions or []:
        eid = m.get("entity_id")
        et = m.get("entity_type")
        if eid:
            entity_ids.append(str(eid))
        if et:
            entity_types.append(str(et))
    ov = ""
    try:
        from app.modules.ontology.semantic import versioning
        ov = str(versioning.current_version().get("ontology_version") or "")
    except Exception:  # noqa: BLE001
        pass
    return {
        "entity_ids": entity_ids,
        "entity_types": sorted(set(entity_types)),
        "ontology_version": ov or None,
    }


def embed_chunks(doc_id, chunks: list[dict], *, run_id: str | None = None,
                 rebuild_from: str | None = None,
                 delete_existing: bool = True) -> int:
    """切片 → 本体标注 → 向量化 → 落库，一处写全所有元数据。

    chunks: [{"content": str, "locator": dict}]。返回实际写入的切片数。
    """
    from app.modules.ingestion import storage
    from app.modules.retrieval import service as retrieval
    from app.modules.retrieval import vector_store

    did = str(doc_id)
    prepared: list[dict] = []
    for idx, ch in enumerate(chunks):
        content = storage.sanitize_text(str(ch.get("content") or ""))[:8000]
        if not content.strip():
            continue
        ann = annotate_chunk(content)
        meta: dict = {
            "chunk_index": idx,
            "locator": ch.get("locator") or {},
            "entity_ids": ann["entity_ids"],
            "entity_types": ann["entity_types"],
            "ontology_version": ann["ontology_version"],
            "embedding_model": settings.embedding_model,
            "embedding_version": EMBEDDING_VERSION,
        }
        if run_id:
            meta["run_id"] = run_id
        if rebuild_from:
            meta["rebuilt_from"] = rebuild_from
        prepared.append({
            "content": content,
            "meta": json.dumps(meta, ensure_ascii=False),
            "ann": ann,
        })

    vec_col = retrieval.ensure_vector_column()
    vector_store.ensure_columns()

    vectors: list = []
    if vec_col and prepared:
        try:
            vectors = llm_client.embed(
                [p["content"][:1500] for p in prepared], timeout=25
            )
        except Exception:  # noqa: BLE001  网关不可用时纯词法检索仍可用
            vectors = []

    has_vec = bool(vectors)
    cols = ("document_id, content, metadata, entity_ids, entity_types, "
            "ontology_version")
    binds = (":d, :c, CAST(:m AS jsonb), CAST(:eids AS jsonb), "
             "CAST(:etypes AS text[]), :ov")
    if vec_col:
        cols += ", embedding_vec, embedding_model, embedding_version, embedded_at"
        binds += ", CAST(:v AS text), :emodel, :ever, now()"

    rows = []
    for i, p in enumerate(prepared):
        ann = p["ann"]
        row = {
            "d": did, "c": p["content"], "m": p["meta"],
            "eids": json.dumps(ann["entity_ids"]),
            "etypes": ann["entity_types"],
            "ov": ann["ontology_version"],
        }
        if vec_col:
            vec = vectors[i] if i < len(vectors) and isinstance(vectors[i], list) else None
            row.update({
                "v": json.dumps(vec) if vec else None,
                "emodel": settings.embedding_model if vec else None,
                "ever": EMBEDDING_VERSION if vec else None,
            })
        rows.append(row)

    # DELETE + INSERT 同一事务：中途失败不留半截切片（比原实现更原子）
    with db.transaction():
        if delete_existing:
            db.execute("DELETE FROM knowledge.semantic_chunks WHERE document_id = :d", {"d": did})
        if rows:
            db.executemany(
                f"INSERT INTO knowledge.semantic_chunks ({cols}) VALUES ({binds})",
                rows,
            )
    return len(prepared)


__all__ = ["embed_chunks", "annotate_chunk", "EMBEDDING_VERSION"]
