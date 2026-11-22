"""Automodel Discovery must never be used as the production ingestion path."""
import pandas as pd
import pytest

from app.modules.ontology.automodel import materializer
from app.modules.ontology.automodel.connectors import Table, TableSet


def test_materialize_marks_output_as_discovery_only():
    source = TableSet(source_id="staff-upload", kind="file")
    source.tables["staff"] = Table(
        name="staff",
        df=pd.DataFrame([{"employee_id": "E-1", "name": "张三"}]),
    )
    contract = {
        "source": "staff-upload",
        "contract_version": "v1",
        "mappings": [{
            "table": "staff",
            "id": "staff:{employee_id}",
            "class": "Employee",
            "name": "name",
            "attributes": {"full_name": "name"},
            "relations": [],
        }],
        "links": [],
    }

    result = materializer.materialize(source, contract)

    assert result["data_scope"] == materializer.DISCOVERY_DATA_SCOPE
    assert result["stats"]["entities"] == 1
    assert result["entities"][0]["external_id"] == "staff:E-1"


@pytest.mark.parametrize("scope", ["discovery_sample", "production", None])
def test_legacy_pg_writer_never_persists_automodel_payload(scope):
    # The old writer must not be made usable by caller-supplied metadata.
    result = {"data_scope": scope, "entities": [], "facts": []}

    with pytest.raises(ValueError, match="mapping_execution"):
        materializer.PgWriter(db=None).write(result)
