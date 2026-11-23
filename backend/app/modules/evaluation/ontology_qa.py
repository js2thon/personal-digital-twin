"""本体问答评测 E1：题库、版本快照、逐题回归。"""
from __future__ import annotations

import hashlib
import inspect
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from app.db import session as db
from app.modules.ontology.modelling import ontology_qa


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def ensure_schema() -> None:
    """幂等创建评测专用表；不触碰业务事实、本体或映射数据。"""
    db.execute("CREATE SCHEMA IF NOT EXISTS ontology")
    statements = [
        """CREATE TABLE IF NOT EXISTS ontology.eval_suites (
            id UUID PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL,
            name VARCHAR(200) NOT NULL, version VARCHAR(64) NOT NULL,
            split VARCHAR(20) NOT NULL DEFAULT 'gold',
            created_by VARCHAR(128), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, name, version))""",
        """CREATE TABLE IF NOT EXISTS ontology.eval_questions (
            id UUID PRIMARY KEY, suite_id UUID NOT NULL REFERENCES ontology.eval_suites(id) ON DELETE CASCADE,
            tenant_id VARCHAR(128) NOT NULL, question TEXT NOT NULL,
            intent VARCHAR(80), tags JSONB NOT NULL DEFAULT '[]'::jsonb,
            expect JSONB NOT NULL, difficulty VARCHAR(20) DEFAULT 'L1',
            source VARCHAR(40) DEFAULT 'manual', active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS ontology.eval_runs (
            id UUID PRIMARY KEY, tenant_id VARCHAR(128) NOT NULL,
            suite_id UUID NOT NULL REFERENCES ontology.eval_suites(id),
            snapshot JSONB NOT NULL, metrics JSONB NOT NULL,
            status VARCHAR(20) NOT NULL, created_by VARCHAR(128),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS ontology.eval_results (
            id UUID PRIMARY KEY, run_id UUID NOT NULL REFERENCES ontology.eval_runs(id) ON DELETE CASCADE,
            tenant_id VARCHAR(128) NOT NULL, question_id UUID NOT NULL,
            passed BOOLEAN NOT NULL, latency_ms DOUBLE PRECISION NOT NULL,
            engine VARCHAR(40), sparql TEXT, rows JSONB NOT NULL DEFAULT '[]'::jsonb,
            answer_text TEXT, attempts JSONB NOT NULL DEFAULT '[]'::jsonb,
            expected JSONB NOT NULL, actual JSONB NOT NULL, error TEXT)""",
        "CREATE INDEX IF NOT EXISTS ix_eval_suites_tenant ON ontology.eval_suites(tenant_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_eval_questions_suite ON ontology.eval_questions(tenant_id, suite_id, active)",
        "CREATE INDEX IF NOT EXISTS ix_eval_runs_tenant ON ontology.eval_runs(tenant_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_eval_results_run ON ontology.eval_results(tenant_id, run_id)",
    ]
    for statement in statements:
        db.execute(statement)


def create_suite(tenant_id: str, actor: str, payload: dict) -> dict:
    ensure_schema()
    name, version = str(payload.get("name") or "").strip(), str(payload.get("version") or "1")
    questions = payload.get("questions") or []
    if not name or len(name) > 200 or not questions:
        raise ValueError("题集名称不能为空，且至少需要一道题")
    if len(questions) > 500:
        raise ValueError("单个题集最多 500 道题")
    split = str(payload.get("split") or "gold")
    if split not in {"gold", "validation", "holdout"}:
        raise ValueError("split 仅支持 gold、validation、holdout")
    suite_id = str(uuid.uuid4())
    with db.transaction():
        db.execute(
            """INSERT INTO ontology.eval_suites(id,tenant_id,name,version,split,created_by)
               VALUES (CAST(:id AS uuid),:tenant,:name,:version,:split,:actor)""",
            {"id": suite_id, "tenant": tenant_id, "name": name, "version": version,
             "split": split, "actor": actor})
        for item in questions:
            q = str(item.get("question") or "").strip()
            expect = item.get("expect")
            if not q or not isinstance(expect, dict) or expect.get("type") not in {
                "answer_set", "sparql_result", "ask_boolean", "min_rows"
            }:
                raise ValueError("每道题必须包含 question 和有效的 expect.type")
            db.execute(
                """INSERT INTO ontology.eval_questions
                   (id,suite_id,tenant_id,question,intent,tags,expect,difficulty,source)
                   VALUES (CAST(:id AS uuid),CAST(:suite AS uuid),:tenant,:question,
                   :intent,CAST(:tags AS jsonb),CAST(:expect AS jsonb),:difficulty,:source)""",
                {"id": str(uuid.uuid4()), "suite": suite_id, "tenant": tenant_id,
                 "question": q, "intent": item.get("intent"),
                 "tags": _json(item.get("tags") or []), "expect": _json(expect),
                 "difficulty": item.get("difficulty") or "L1",
                 "source": item.get("source") or "manual"})
    return {"id": suite_id, "name": name, "version": version,
            "split": split, "question_count": len(questions)}


def list_suites(tenant_id: str) -> list[dict]:
    ensure_schema()
    return db.fetch_all(
        """SELECT s.id::text AS id,s.name,s.version,s.split,s.created_at,
                  count(q.id)::int AS question_count
           FROM ontology.eval_suites s LEFT JOIN ontology.eval_questions q
             ON q.suite_id=s.id AND q.tenant_id=s.tenant_id AND q.active
           WHERE s.tenant_id=:tenant
           GROUP BY s.id ORDER BY s.created_at DESC""", {"tenant": tenant_id})


def _actual(out: dict) -> dict:
    result = out.get("result") or {}
    return {"kind": result.get("kind") or "SELECT",
            "vars": result.get("vars") or [], "rows": result.get("rows") or [],
            "boolean": result.get("boolean"), "answer": out.get("answer") or ""}


def _cell(row: Any) -> list:
    if isinstance(row, dict):
        return [row[k] for k in sorted(row)]
    return list(row) if isinstance(row, (list, tuple)) else [row]


def _compare(expect: dict, actual: dict) -> bool:
    kind = expect["type"]
    if kind == "ask_boolean":
        return actual["kind"] == "ASK" and actual["boolean"] is bool(expect.get("value"))
    if kind == "min_rows":
        return len(actual["rows"]) >= int(expect.get("value", 0))
    if kind == "answer_set":
        wanted = {str(x) for x in (expect.get("values") or [])}
        found = {str(v) for row in actual["rows"] for v in _cell(row)}
        return wanted <= found if expect.get("mode") == "contains" else wanted == found
    if kind == "sparql_result":
        expected_rows = sorted(_json(_cell(row)) for row in (expect.get("rows") or []))
        actual_rows = sorted(_json(_cell(row)) for row in actual["rows"])
        return (not expect.get("vars") or list(expect["vars"]) == actual["vars"]) and expected_rows == actual_rows
    return False


def run_suite(tenant_id: str, actor: str, suite_id: str, spec: dict,
              data: dict | None = None, snapshot: dict | None = None,
              use_llm: bool = True) -> dict:
    ensure_schema()
    suite = db.fetch_one(
        "SELECT id::text AS id,name,version,split FROM ontology.eval_suites "
        "WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant",
        {"id": suite_id, "tenant": tenant_id})
    if not suite:
        raise LookupError("题集不存在或不属于当前租户")
    questions = db.fetch_all(
        """SELECT id::text AS id,question,intent,expect,tags,difficulty,source
           FROM ontology.eval_questions WHERE suite_id=CAST(:id AS uuid)
             AND tenant_id=:tenant AND active ORDER BY created_at,id""",
        {"id": suite_id, "tenant": tenant_id})
    if not questions:
        raise ValueError("题集没有启用中的题目")
    if len(questions) > 500:
        raise ValueError("单次评测最多 500 道题")

    started = time.perf_counter()
    results = []
    for q in questions:
        t0 = time.perf_counter()
        out = ontology_qa.answer(spec, q["question"], data=data, use_llm=use_llm)
        latency = round((time.perf_counter() - t0) * 1000, 3)
        actual = _actual(out)
        expect = q["expect"] if isinstance(q["expect"], dict) else json.loads(q["expect"])
        passed = bool(out.get("ok")) and _compare(expect, actual)
        results.append({
            "question_id": q["id"], "question": q["question"], "passed": passed,
            "latency_ms": latency, "engine": out.get("engine"),
            "sparql": out.get("sparql") or "", "rows": actual["rows"],
            "answer_text": actual["answer"], "attempts": out.get("attempts") or [],
            "expected": expect, "actual": actual,
            "error": out.get("error"),
        })

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    module_source = inspect.getsource(ontology_qa)
    supplied = snapshot or {}
    snap = {
        "ontology_version": supplied.get("ontology_version"),
        "mapping_version": supplied.get("mapping_version"),
        "dataset_batch_ids": supplied.get("dataset_batch_ids") or [],
        "ontology_spec_hash": _hash(spec),
        "data_hash": _hash(data or {}),
        "qa_engine_version": _hash(module_source)[:16],
        "prompt_version": supplied.get("prompt_version") or "embedded",
        "suite_id": suite["id"], "suite_version": suite["version"],
        "split": suite["split"],
    }
    run_id = str(uuid.uuid4())
    metrics = {"total": total, "passed": passed, "failed": total - passed,
               "accuracy": round(passed / total, 6), "latency_ms": elapsed,
               "executable_rate": round(sum(bool(r["actual"]["kind"]) for r in results) / total, 6)}
    with db.transaction():
        db.execute(
            """INSERT INTO ontology.eval_runs(id,tenant_id,suite_id,snapshot,metrics,status,created_by)
               VALUES (CAST(:id AS uuid),:tenant,CAST(:suite AS uuid),CAST(:snapshot AS jsonb),
                       CAST(:metrics AS jsonb),'completed',:actor)""",
            {"id": run_id, "tenant": tenant_id, "suite": suite_id,
             "snapshot": _json(snap), "metrics": _json(metrics), "actor": actor})
        for r in results:
            db.execute(
                """INSERT INTO ontology.eval_results
                   (id,run_id,tenant_id,question_id,passed,latency_ms,engine,sparql,rows,
                    answer_text,attempts,expected,actual,error)
                   VALUES (CAST(:id AS uuid),CAST(:run AS uuid),:tenant,CAST(:question AS uuid),
                    :passed,:latency,:engine,:sparql,CAST(:rows AS jsonb),:answer,
                    CAST(:attempts AS jsonb),CAST(:expected AS jsonb),CAST(:actual AS jsonb),:error)""",
                {"id": str(uuid.uuid4()), "run": run_id, "tenant": tenant_id,
                 "question": r["question_id"], "passed": r["passed"],
                 "latency": r["latency_ms"], "engine": r["engine"], "sparql": r["sparql"],
                 "rows": _json(r["rows"]), "answer": r["answer_text"],
                 "attempts": _json(r["attempts"]), "expected": _json(r["expected"]),
                 "actual": _json(r["actual"]), "error": r["error"]})
    return {"run_id": run_id, "suite": suite, "snapshot": snap,
            "metrics": metrics, "results": results,
            "created_at": datetime.now(timezone.utc).isoformat()}
