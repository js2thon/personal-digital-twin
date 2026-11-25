"""本体工作台：数据集落库 + 本体版本管理。

两张表（与 automodel_jobs 同库同 schema）：
- ontology.automodel_datasets  上传/拉取的原始表数据（gzip 压缩 JSON，含主键/外键/注释），按内容 sha256 去重；
  服务重启或内存 TTL 过期后，_source() 从这里恢复 TableSet，不必重新上传。
- ontology.automodel_versions  任务快照：draft(本体+图关系+审核) / contract(映射) / metrics / actions / 手工配置，
  版本号在任务内自增；支持列出、查看、两版差异、回滚到任一版本、基于已存数据集新建任务复用。
数据库连接的只是来源描述（已脱敏），不存连接串明文；数据库来源默认不落原始数据（store_db_data=False）。
无 DB（单测/离线）时退回进程内字典，接口一致。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import threading
import time
import uuid

import pandas as pd

from . import connectors

_LOCK = threading.Lock()
_MEM_DS: dict[str, dict] = {}
_MEM_V: dict[str, dict] = {}
_ensured = False
MAX_DATASET_BYTES = 80 * 1024 * 1024

DDL = [
    "CREATE SCHEMA IF NOT EXISTS ontology",
    """CREATE TABLE IF NOT EXISTS ontology.automodel_datasets (
        id VARCHAR(40) PRIMARY KEY,
        tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
        name VARCHAR(200) NOT NULL,
        source_id VARCHAR(120) NOT NULL,
        kind VARCHAR(20) NOT NULL,
        sha256 CHAR(64) NOT NULL,
        tables JSONB NOT NULL DEFAULT '[]',     -- [{name, rows, cols}]
        payload BYTEA NOT NULL,
        size_bytes BIGINT NOT NULL,
        created_by VARCHAR(120) NOT NULL DEFAULT 'owner',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_automodel_datasets_sha ON ontology.automodel_datasets (tenant_id, sha256)",
    """CREATE TABLE IF NOT EXISTS ontology.automodel_versions (
        id VARCHAR(40) PRIMARY KEY,
        tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
        job_id VARCHAR(40) NOT NULL,
        dataset_id VARCHAR(40),
        version_no INT NOT NULL,
        label VARCHAR(200) NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        trigger VARCHAR(30) NOT NULL DEFAULT 'manual',  -- manual/upload/review/configure/publish/restore
        summary JSONB NOT NULL DEFAULT '{}',
        snapshot JSONB NOT NULL,
        parent_id VARCHAR(40),
        created_by VARCHAR(120) NOT NULL DEFAULT 'owner',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (job_id, version_no))""",
    "CREATE INDEX IF NOT EXISTS idx_automodel_versions_job ON ontology.automodel_versions (tenant_id, job_id, version_no DESC)",
    "ALTER TABLE ontology.automodel_jobs ADD COLUMN IF NOT EXISTS dataset_id VARCHAR(40)",
]


def _db():
    try:
        from app.db import session as db
        return db
    except Exception:  # noqa: BLE001
        return None


def ensure_schema() -> bool:
    global _ensured
    db = _db()
    if db is None:
        return False
    if not _ensured:
        from . import jobs
        jobs.ensure_schema()
        for ddl in DDL:
            db.execute(ddl)
        _ensured = True
    return True


def _j(v):
    return v if isinstance(v, (dict, list)) or v is None else json.loads(v)


# ------------------------------------------------------------------ 数据集
def _pack(ts: connectors.TableSet) -> bytes:
    doc = {"source_id": ts.source_id, "kind": ts.kind, "tables": []}
    for t in ts.tables.values():
        buf = io.StringIO()
        t.df.to_json(buf, orient="split", date_format="iso", force_ascii=False, index=False)
        doc["tables"].append({
            "name": t.name, "df": buf.getvalue(), "dtypes": {c: str(d) for c, d in t.df.dtypes.items()},
            "primary_key": t.primary_key, "comment": t.comment, "column_comments": t.column_comments,
            "column_labels": t.column_labels, "column_roles": t.column_roles,
            "row_count": t.row_count,
            "foreign_keys": [{"column": fk.column, "ref_table": fk.ref_table, "ref_column": fk.ref_column,
                              "declared": fk.declared} for fk in t.foreign_keys]})
    return gzip.compress(json.dumps(doc, ensure_ascii=False).encode("utf-8"), 6, mtime=0)


def _unpack(blob: bytes) -> connectors.TableSet:
    doc = json.loads(gzip.decompress(bytes(blob)).decode("utf-8"))
    ts = connectors.TableSet(source_id=doc["source_id"], kind=doc["kind"])
    for t in doc["tables"]:
        df = pd.read_json(io.StringIO(t["df"]), orient="split", convert_dates=False, dtype=False)
        for c, d in (t.get("dtypes") or {}).items():
            if c in df.columns:
                try:
                    df[c] = pd.to_datetime(df[c]) if d.startswith("datetime") else df[c].astype(d)
                except Exception:  # noqa: BLE001  类型还原失败就保留读入类型
                    pass
        ts.tables[t["name"]] = connectors.Table(
            name=t["name"], df=df, primary_key=t.get("primary_key") or [], comment=t.get("comment") or "",
            column_comments=t.get("column_comments") or {}, column_labels=t.get("column_labels") or {},
            column_roles=t.get("column_roles") or {}, row_count=t.get("row_count"),
            foreign_keys=[connectors.ForeignKey(**fk) for fk in t.get("foreign_keys") or []])
    return ts


def save_dataset(ts: connectors.TableSet, name: str, tenant: str = "default", actor: str = "owner") -> dict:
    blob = _pack(ts)
    if len(blob) > MAX_DATASET_BYTES:
        return {"id": None, "skipped": f"压缩后 {len(blob) // 1048576}MB 超过落库上限"}
    sha = hashlib.sha256(blob).hexdigest()
    meta = [{"name": t.name, "rows": int(len(t.df)), "cols": int(t.df.shape[1])} for t in ts.tables.values()]
    if not ensure_schema():
        with _LOCK:
            hit = next((d for d in _MEM_DS.values() if d["sha256"] == sha and d["tenant_id"] == tenant), None)
            if hit:
                return {**{k: v for k, v in hit.items() if k != "payload"}, "dedup": True}
            d = {"id": uuid.uuid4().hex[:16], "tenant_id": tenant, "name": name, "source_id": ts.source_id,
                 "kind": ts.kind, "sha256": sha, "tables": meta, "payload": blob, "size_bytes": len(blob),
                 "created_by": actor, "created_at": time.time()}
            _MEM_DS[d["id"]] = d
            return {k: v for k, v in d.items() if k != "payload"}
    db = _db()
    row = db.fetch_one("SELECT id FROM ontology.automodel_datasets WHERE tenant_id=:t AND sha256=:s",
                       {"t": tenant, "s": sha})
    if row:
        return {"id": row["id"], "dedup": True, "tables": meta, "size_bytes": len(blob)}
    did = uuid.uuid4().hex[:16]
    db.execute("INSERT INTO ontology.automodel_datasets (id, tenant_id, name, source_id, kind, sha256, tables, "
               "payload, size_bytes, created_by) VALUES (:i,:t,:n,:s,:k,:h,CAST(:tb AS jsonb),:p,:z,:a) "
               "ON CONFLICT (tenant_id, sha256) DO NOTHING",
               {"i": did, "t": tenant, "n": name[:200], "s": ts.source_id, "k": ts.kind, "h": sha,
                "tb": json.dumps(meta, ensure_ascii=False), "p": blob, "z": len(blob), "a": actor})
    row = db.fetch_one("SELECT id FROM ontology.automodel_datasets WHERE tenant_id=:t AND sha256=:s",
                       {"t": tenant, "s": sha})
    return {"id": row["id"], "dedup": row["id"] != did, "tables": meta, "size_bytes": len(blob)}


def load_dataset(dataset_id: str, tenant: str = "default") -> connectors.TableSet | None:
    if not ensure_schema():
        d = _MEM_DS.get(dataset_id)
        return _unpack(d["payload"]) if d and d["tenant_id"] == tenant else None
    row = _db().fetch_one("SELECT payload FROM ontology.automodel_datasets WHERE id=:i AND tenant_id=:t",
                          {"i": dataset_id, "t": tenant})
    return _unpack(row["payload"]) if row else None


def list_datasets(tenant: str = "default", limit: int = 100) -> list[dict]:
    if not ensure_schema():
        return [{k: v for k, v in d.items() if k != "payload"} for d in _MEM_DS.values()
                if d["tenant_id"] == tenant][-limit:]
    rows = _db().fetch_all(
        "SELECT d.id, d.name, d.source_id, d.kind, d.tables, d.size_bytes, d.created_by, d.created_at, "
        " (SELECT count(*) FROM ontology.automodel_jobs j WHERE j.dataset_id=d.id) AS jobs "
        "FROM ontology.automodel_datasets d WHERE d.tenant_id=:t ORDER BY d.created_at DESC LIMIT :n",
        {"t": tenant, "n": max(1, min(limit, 500))})
    for r in rows:
        r["tables"] = _j(r["tables"])
    return rows


def delete_dataset(dataset_id: str, tenant: str = "default") -> bool:
    if not ensure_schema():
        return _MEM_DS.pop(dataset_id, None) is not None
    _db().execute("DELETE FROM ontology.automodel_datasets WHERE id=:i AND tenant_id=:t",
                  {"i": dataset_id, "t": tenant})
    return True


def rename_dataset(dataset_id: str, name: str, tenant: str = "default") -> dict | None:
    """重命名数据集（仅改展示名，不动内容与 sha256 去重键）。"""
    if not ensure_schema():
        d = _MEM_DS.get(dataset_id)
        if not d or d["tenant_id"] != tenant:
            return None
        d["name"] = name[:200]
        return {k: v for k, v in d.items() if k != "payload"}
    row = _db().fetch_one(
        "UPDATE ontology.automodel_datasets SET name=:n WHERE id=:i AND tenant_id=:t "
        "RETURNING id, name, source_id, kind, tables, size_bytes, created_by, created_at",
        {"n": name[:200], "i": dataset_id, "t": tenant})
    if row:
        row["tables"] = _j(row["tables"])
    return row


def dataset_metadata(dataset_id: str, tenant: str = "default") -> dict | None:
    """提取数据集元数据：表清单 + 每表列画像（类型/空值率/去重数/样例）。"""
    ts = load_dataset(dataset_id, tenant)
    if ts is None:
        return None
    tables = []
    for t in ts.tables.values():
        df = t.df
        cols = []
        for c in df.columns:
            s = df[c]
            nulls = int(s.isna().sum())
            distinct = int(s.nunique(dropna=True))
            samples = [str(v) for v in s.dropna().unique()[:3]]
            cols.append({
                "name": str(c),
                "dtype": str(s.dtype),
                "rows": int(len(s)),
                "null_ratio": round(nulls / max(len(s), 1), 4),
                "distinct": distinct,
                "samples": samples,
                "role": t.column_roles.get(str(c), "") or ("identifier" if c in (t.primary_key or []) else ""),
                "label": t.column_labels.get(str(c), ""),
                "comment": t.column_comments.get(str(c), ""),
            })
        tables.append({
            "name": t.name,
            "rows": int(len(df)),
            "cols": int(df.shape[1]),
            "primary_key": list(t.primary_key or []),
            "foreign_keys": [{"column": fk.column, "ref_table": fk.ref_table,
                              "ref_column": fk.ref_column} for fk in t.foreign_keys],
            "comment": t.comment,
            "columns": cols,
        })
    return {"dataset_id": dataset_id, "source_id": ts.source_id, "kind": ts.kind,
            "tables": tables}


def link_job(job_id: str, dataset_id: str | None) -> None:
    if dataset_id and ensure_schema():
        _db().execute("UPDATE ontology.automodel_jobs SET dataset_id=:d WHERE id=:i", {"d": dataset_id, "i": job_id})


def job_dataset(job_id: str) -> str | None:
    if not ensure_schema():
        return None
    row = _db().fetch_one("SELECT dataset_id FROM ontology.automodel_jobs WHERE id=:i", {"i": job_id})
    return row and row.get("dataset_id")


# ------------------------------------------------------------------ 版本
SNAP_KEYS = ("profiles", "draft", "contract", "issues", "stages", "metrics", "actions_log", "config", "schema_version", "design_layout", "business_object_discovery", "standards_report", "release_gate", "metadata_revision", "metadata_sync")


def _snapshot(job: dict) -> dict:
    return json.loads(json.dumps({k: job.get(k) for k in SNAP_KEYS}, ensure_ascii=False, default=str))


def _summary(snap: dict) -> dict:
    sc = ((snap.get("draft") or {}).get("schema") or {})
    et, rt = sc.get("entity_types") or {}, sc.get("relation_types") or {}
    ms = ((snap.get("metrics") or {}).get("metrics") or {})
    return {"classes": len(et), "relations": len(rt),
            "attributes": len(sc.get("attributes") or {}),
            "metrics": len(ms), "review": len((snap.get("draft") or {}).get("review") or []),
            "mappings": len(((snap.get("contract") or {}).get("mappings") or [])),
            "schema_version": snap.get("schema_version")}


def create(job: dict, tenant: str = "default", actor: str = "owner", label: str = "", note: str = "",
           trigger: str = "manual", dataset_id: str | None = None, dedupe: bool = True) -> dict:
    snap = _snapshot(job)
    summ = _summary(snap)
    prev = latest(job["id"], tenant, with_snapshot=True)
    if dedupe and prev and json.dumps(prev["snapshot"], sort_keys=True, default=str) == \
            json.dumps(snap, sort_keys=True, default=str):
        return {**{k: v for k, v in prev.items() if k != "snapshot"}, "unchanged": True}
    no = (prev["version_no"] + 1) if prev else 1
    v = {"id": uuid.uuid4().hex[:16], "tenant_id": tenant, "job_id": job["id"], "dataset_id": dataset_id,
         "version_no": no, "label": (label or f"v{no}")[:200], "note": note or "", "trigger": trigger,
         "summary": summ, "parent_id": prev["id"] if prev else None, "created_by": actor}
    if not ensure_schema():
        with _LOCK:
            _MEM_V[v["id"]] = {**v, "snapshot": snap, "created_at": time.time()}
        return v
    _db().execute(
        "INSERT INTO ontology.automodel_versions (id, tenant_id, job_id, dataset_id, version_no, label, note, "
        " trigger, summary, snapshot, parent_id, created_by) VALUES (:id,:tenant_id,:job_id,:dataset_id,"
        " :version_no,:label,:note,:trigger,CAST(:s AS jsonb),CAST(:snap AS jsonb),:parent_id,:created_by)",
        {**v, "s": json.dumps(summ, ensure_ascii=False), "snap": json.dumps(snap, ensure_ascii=False, default=str)})
    return v


def listing(job_id: str, tenant: str = "default") -> list[dict]:
    if not ensure_schema():
        return sorted([{k: x for k, x in v.items() if k != "snapshot"} for v in _MEM_V.values()
                       if v["job_id"] == job_id and v["tenant_id"] == tenant], key=lambda r: -r["version_no"])
    rows = _db().fetch_all(
        "SELECT id, job_id, dataset_id, version_no, label, note, trigger, summary, parent_id, created_by, "
        " created_at FROM ontology.automodel_versions WHERE tenant_id=:t AND job_id=:j ORDER BY version_no DESC",
        {"t": tenant, "j": job_id})
    for r in rows:
        r["summary"] = _j(r["summary"])
    return rows


def get(version_id: str, tenant: str = "default") -> dict | None:
    if not ensure_schema():
        v = _MEM_V.get(version_id)
        return v if v and v["tenant_id"] == tenant else None
    row = _db().fetch_one("SELECT * FROM ontology.automodel_versions WHERE id=:i AND tenant_id=:t",
                          {"i": version_id, "t": tenant})
    if row:
        row["snapshot"], row["summary"] = _j(row["snapshot"]), _j(row["summary"])
    return row


def latest(job_id: str, tenant: str = "default", with_snapshot: bool = False) -> dict | None:
    rows = listing(job_id, tenant)
    if not rows:
        return None
    return get(rows[0]["id"], tenant) if with_snapshot else rows[0]


def update_meta(version_id: str, tenant: str, label: str | None, note: str | None) -> None:
    if not ensure_schema():
        v = _MEM_V[version_id]
        v["label"] = label if label is not None else v["label"]
        v["note"] = note if note is not None else v["note"]
        return
    _db().execute("UPDATE ontology.automodel_versions SET label=COALESCE(:l,label), note=COALESCE(:n,note) "
                  "WHERE id=:i AND tenant_id=:t", {"l": label, "n": note, "i": version_id, "t": tenant})


def apply(job: dict, version: dict) -> dict:
    """把快照写回任务（回滚）；物化结果作废，下次按快照契约重新物化。"""
    for k, v in (version["snapshot"] or {}).items():
        job[k] = v
    job.pop("materialized", None)
    return job


def diff(a: dict, b: dict) -> dict:
    """两个版本的本体差异：类 / 属性 / 关系 / 映射 / 指标 的增删改。"""
    def parts(snap):
        sc = ((snap.get("draft") or {}).get("schema") or {})
        et, rt = sc.get("entity_types") or {}, sc.get("relation_types") or {}
        attrs = {f"{(d or {}).get('owner', '')}.{a}": json.dumps(d, sort_keys=True, default=str)
                 for a, d in (sc.get("attributes") or {}).items()}
        cls = {c: json.dumps(v, sort_keys=True, default=str) for c, v in et.items()}
        rel = {r: json.dumps(v, sort_keys=True, default=str) for r, v in rt.items()}
        mp = {m.get("class"): json.dumps(m, sort_keys=True, default=str)
              for m in ((snap.get("contract") or {}).get("mappings") or [])}
        ms = {k: json.dumps(v, sort_keys=True, default=str)
              for k, v in (((snap.get("metrics") or {}).get("metrics")) or {}).items()}
        return {"classes": cls, "attributes": attrs, "relations": rel, "mappings": mp, "metrics": ms}

    pa, pb = parts(a["snapshot"]), parts(b["snapshot"])
    out = {}
    for k in pa:
        x, y = pa[k], pb[k]
        out[k] = {"added": sorted(set(y) - set(x)), "removed": sorted(set(x) - set(y)),
                  "changed": sorted(i for i in set(x) & set(y) if x[i] != y[i])}
    out["total"] = sum(len(v["added"]) + len(v["removed"]) + len(v["changed"]) for v in out.values()
                       if isinstance(v, dict))
    return out
