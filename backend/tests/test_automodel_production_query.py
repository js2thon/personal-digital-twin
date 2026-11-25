from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.automodel import _production_snapshot


def test_production_snapshot_reads_version_pinned_visible_instances(monkeypatch):
    from app import policy
    from app.db import session as db
    from app.modules.ontology.semantic import service, store

    def fetch_one(sql, params):
        if "entity_instances" in sql:
            return {"n": 1}
        if "instance_relations" in sql:
            return {"n": 1}
        raise AssertionError(sql)

    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(service, "ensure_ready", lambda: None)
    monkeypatch.setattr(store, "list_instances", lambda **kwargs: [{
        "id": "instance-1", "type_name": "Person", "canonical_name": "张三",
        "properties": {"name": "张三"}, "ontology_version": "v2.0.1",
    }])
    monkeypatch.setattr(store, "list_relations", lambda **kwargs: [{
        "source_id": "instance-1", "target_id": "instance-1",
        "relation_type": "reports_to", "derived": False,
    }])
    monkeypatch.setattr(policy, "filter_rows", lambda rows, principal: rows)

    job = {
        "schema_version": "v2.0.1",
        "draft": {"schema": {
            "entity_types": {"Person": {}},
            "relation_types": {"reports_to": {"label": "汇报给"}},
        }},
    }
    request = SimpleNamespace(state=SimpleNamespace(principal={"tenant_id": "t1"}))

    materialized, evidence = _production_snapshot(job, request, limit=10)

    assert materialized["entities"][0]["external_id"] == "instance-1"
    assert materialized["facts"][0]["predicate"] == "reports_to"
    assert evidence["execution_source"] == (
        "ontology.entity_instances+ontology.instance_relations"
    )
    assert evidence["complete"] is True


def test_production_snapshot_rejects_truncated_relation_reads(monkeypatch):
    from app.db import session as db
    from app.modules.ontology.semantic import service, store

    def fetch_one(sql, params):
        if "entity_instances" in sql:
            return {"n": 1}
        if "instance_relations" in sql:
            return {"n": 6}
        raise AssertionError(sql)

    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(service, "ensure_ready", lambda: None)
    monkeypatch.setattr(store, "list_instances", lambda **kwargs: [])
    job = {
        "schema_version": "v2.0.1",
        "draft": {"schema": {"entity_types": {}, "relation_types": {}}},
    }
    request = SimpleNamespace(state=SimpleNamespace(principal={}))

    with pytest.raises(HTTPException) as exc:
        _production_snapshot(job, request, limit=1)

    assert exc.value.status_code == 409
    assert "relations_available" in exc.value.detail


from app.api.v1.automodel import _production_frames


def test_production_frames_include_empty_classes_and_relations():
    schema = {
        "entity_types": {
            "Person": {"attributes": [{"name": "name"}]},
            "Department": {"attributes": [{"name": "title"}]},
        },
        "relation_types": {
            "works_at": {
                "source": "Person", "target": "Department",
                "cardinality": "many_to_one",
            },
            "collaborates_with": {
                "source": "Person", "target": "Person",
                "cardinality": "many_to_many",
            },
        },
    }

    frames, links = _production_frames({"entities": [], "facts": []}, schema)

    assert list(frames["Person"].columns) == ["_id", "_name", "name", "works_at"]
    assert list(frames["Department"].columns) == ["_id", "_name", "title"]
    assert list(links["collaborates_with"].columns) == ["source", "target"]
