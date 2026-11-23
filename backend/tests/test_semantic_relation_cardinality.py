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
