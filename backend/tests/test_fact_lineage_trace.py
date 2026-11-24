"""Fact-to-Claim evidence traceability tests."""

from app.api.v1.lineage import _fact_traceability


def test_fact_traceability_distinguishes_claim_and_evidence():
    assert _fact_traceability([{"id": "c1"}], [{"evidence_id": "e1"}], {}) == "claim_and_evidence"
    assert _fact_traceability([{"id": "c1"}], [], {}) == "claim_without_evidence"


def test_fact_traceability_accepts_complete_structured_source():
    source_ref = {
        "channel": "structured_sync",
        "table": "employees",
        "pk": "E-1",
        "row_hash": "hash",
        "batch_id": "batch-1",
        "mapping_version": "3",
        "idempotency_key": "key-1",
    }
    assert _fact_traceability([], [], source_ref) == "structured_source"


def test_fact_traceability_does_not_invent_missing_source():
    assert _fact_traceability([], [], {"channel": "structured_sync", "table": "employees"}) == "unlinked"
