"""Unit tests for deterministic bounded ingestion helpers."""
import pytest

from app.modules.ontology.ingestion_batches import _chunks, _digest


def test_chunks_preserve_order_and_boundaries():
    rows = [{"id": str(i)} for i in range(7)]
    batches = list(_chunks(rows, 3))
    assert [(i, len(batch)) for i, batch in batches] == [(0, 3), (1, 3), (2, 1)]
    assert [row["id"] for _, batch in batches for row in batch] == [str(i) for i in range(7)]


def test_batch_digest_is_deterministic_and_content_sensitive():
    a = _digest({"index": 0, "rows": [{"id": "1"}]})
    assert a == _digest({"index": 0, "rows": [{"id": "1"}]})
    assert a != _digest({"index": 1, "rows": [{"id": "1"}]})
    assert a != _digest({"index": 0, "rows": [{"id": "2"}]})


def test_chunks_reject_non_object_rows():
    with pytest.raises(ValueError, match="对象/字典"):
        list(_chunks([{"id": 1}, "bad"], 1))
