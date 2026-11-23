from app.modules.ontology.semantic.constraints import validate_relation


def _schema(cardinality: str) -> dict:
    return {
        "entity_types": {"Person": {}, "Department": {}},
        "relation_types": {
            "assigned_to": {
                "source_types": ["Person"],
                "target_types": ["Department"],
                "cardinality": cardinality,
            }
        },
    }


def _edge(source: str, target: str) -> dict:
    return {
        "source_id": source,
        "target_id": target,
        "relation_type": "assigned_to",
        "status": "active",
    }


def _validate(cardinality: str, source: str, target: str,
              existing: list[dict]) -> dict:
    return validate_relation(
        _schema(cardinality),
        {
            "source_id": source,
            "target_id": target,
            "relation_type": "assigned_to",
            "source_type": "Person",
            "target_type": "Department",
        },
        existing_edges=existing,
    )


def test_many_to_one_limits_targets_per_source_not_sources_per_target():
    assert _validate("many_to_one", "p1", "d2", [_edge("p1", "d1")])["blocking"]
    assert not _validate("many_to_one", "p2", "d1", [_edge("p1", "d1")])["blocking"]


def test_one_to_many_limits_sources_per_target_not_targets_per_source():
    assert _validate("one_to_many", "p1", "d2", [_edge("p1", "d1")])["blocking"]
    assert not _validate("one_to_many", "p2", "d1", [_edge("p1", "d1")])["blocking"]


def test_one_to_one_limits_both_ends():
    assert _validate("one_to_one", "p1", "d2", [_edge("p1", "d1")])["blocking"]
    assert _validate("one_to_one", "p2", "d1", [_edge("p1", "d1")])["blocking"]


def test_many_to_many_does_not_limit_either_end():
    existing = [_edge("p1", "d1")]
    assert not _validate("many_to_many", "p1", "d2", existing)["blocking"]
    assert not _validate("many_to_many", "p2", "d1", existing)["blocking"]


def test_store_lookup_is_tenant_and_relation_scoped(monkeypatch):
    from app.modules.ontology.semantic import store

    captured = {}

    def fake_fetch_all(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(store.db, "fetch_all", fake_fetch_all)
    assert store.find_relation_cardinality_conflicts(
        "tenant-a", "assigned_to", "source-1", "target-1", "one_to_one"
    ) == []
    assert "tenant_id = :t" in captured["sql"]
    assert "relation_type = :rt" in captured["sql"]
    assert "derived = false" in captured["sql"]
    assert "status = 'active'" in captured["sql"]
    assert "source_id = CAST(:s AS uuid)" in captured["sql"]
    assert "target_id = CAST(:tgt AS uuid)" in captured["sql"]
    assert captured["params"]["t"] == "tenant-a"


def test_write_relation_rejects_missing_tenant_scoped_endpoint(monkeypatch):
    import pytest
    from contextlib import nullcontext
    from app.modules.ontology.semantic import service as service_module
    from app.modules.ontology.semantic.constraints import ConstraintViolation

    monkeypatch.setattr(service_module.db, "transaction", lambda: nullcontext())
    monkeypatch.setattr(service_module.versioning, "current_version",
                        lambda: {"ontology_version": "test-v1"})
    monkeypatch.setattr(service_module.store, "get_instance",
                        lambda instance_id, tenant_id: None)
    svc = service_module.OntologyService(
        {"entity_types": {}, "relation_types": {}}, tenant_id="tenant-a"
    )
    monkeypatch.setattr(svc, "_audit", lambda *args, **kwargs: None)

    with pytest.raises(ConstraintViolation):
        svc.write_relation({
            "source_id": "missing-source",
            "target_id": "missing-target",
            "relation_type": "assigned_to",
            "source_type": "Person",
            "target_type": "Department",
        }, auto_reasoning=False)


def test_write_relation_uses_persisted_endpoint_types_not_request_types(monkeypatch):
    from contextlib import nullcontext
    from app.modules.ontology.semantic import service as service_module

    monkeypatch.setattr(service_module.db, "transaction", lambda: nullcontext())
    monkeypatch.setattr(service_module.versioning, "current_version",
                        lambda: {"ontology_version": "test-v1"})
    instances = {
        "source-1": {"id": "source-1", "type_name": "Person"},
        "target-1": {"id": "target-1", "type_name": "Department"},
    }
    monkeypatch.setattr(service_module.store, "get_instance",
                        lambda instance_id, tenant_id: instances.get(instance_id))
    monkeypatch.setattr(service_module.store, "find_relation_cardinality_conflicts",
                        lambda *args, **kwargs: [])
    monkeypatch.setattr(service_module.store, "lock_relation_cardinality",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(service_module.store, "insert_relation", lambda rec: "relation-1")
    monkeypatch.setattr(service_module, "evaluate_for_write",
                        lambda *args, **kwargs: {"violations": [], "applied_rules": [],
                                                 "derived_candidates": []})
    observed = {}

    def fake_validate(schema, payload, existing_edges=None):
        observed.update(payload)
        return {"blocking": False, "violations": []}

    monkeypatch.setattr(service_module, "validate_relation", fake_validate)
    svc = service_module.OntologyService(
        {"entity_types": {}, "relation_types": {
            "assigned_to": {"cardinality": "many_to_one"}
        }}, tenant_id="tenant-a"
    )
    monkeypatch.setattr(svc, "_audit", lambda *args, **kwargs: None)
    svc.write_relation({
        "source_id": "source-1",
        "target_id": "target-1",
        "relation_type": "assigned_to",
        "source_type": "ForgedSourceType",
        "target_type": "ForgedTargetType",
    }, auto_reasoning=False)

    assert observed["source_type"] == "Person"
    assert observed["target_type"] == "Department"


def test_write_relation_rejects_endpoint_without_persisted_type(monkeypatch):
    import pytest
    from app.modules.ontology.semantic import service as service_module
    from app.modules.ontology.semantic.constraints import ConstraintViolation

    monkeypatch.setattr(service_module.versioning, "current_version",
                        lambda: {"ontology_version": "test-v1"})
    instances = {
        "source-1": {"id": "source-1", "type_name": ""},
        "target-1": {"id": "target-1", "type_name": "Department"},
    }
    monkeypatch.setattr(service_module.store, "get_instance",
                        lambda instance_id, tenant_id: instances.get(instance_id))
    svc = service_module.OntologyService(
        {"entity_types": {}, "relation_types": {}}, tenant_id="tenant-a"
    )
    monkeypatch.setattr(svc, "_audit", lambda *args, **kwargs: None)

    with pytest.raises(ConstraintViolation):
        svc.write_relation({
            "source_id": "source-1",
            "target_id": "target-1",
            "relation_type": "assigned_to",
            "source_type": "Person",
            "target_type": "Department",
        }, auto_reasoning=False)
