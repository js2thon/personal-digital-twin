from app.modules.evaluation import ontology_qa as ev


def test_answer_set_compares_values_not_cell_objects():
    actual = {"ok": True, "kind": "SELECT", "vars": ["name"],
              "rows": [[{"v": "Alice", "k": "literal"}]],
              "boolean": None, "answer": "Alice"}
    assert ev._compare({"type": "answer_set", "values": ["Alice"]}, actual)


def test_answer_set_contains_mode():
    actual = {"ok": True, "kind": "SELECT", "vars": ["name"],
              "rows": [[{"v": "Alice"}], [{"v": "Bob"}]],
              "boolean": None, "answer": ""}
    assert ev._compare({"type": "answer_set", "values": ["Alice"],
                        "mode": "contains"}, actual)
    assert not ev._compare({"type": "answer_set", "values": ["Carol"],
                            "mode": "contains"}, actual)


def test_ask_boolean_requires_ask_result():
    actual = {"ok": True, "kind": "ASK", "vars": [], "rows": [],
              "boolean": True, "answer": "是的"}
    assert ev._compare({"type": "ask_boolean", "value": True}, actual)
    assert not ev._compare({"type": "ask_boolean", "value": False}, actual)
    actual["kind"] = "SELECT"
    assert not ev._compare({"type": "ask_boolean", "value": True}, actual)


def test_min_rows_and_sparql_result():
    actual = {"ok": True, "kind": "SELECT", "vars": ["name"],
              "rows": [[{"v": "Alice"}]], "boolean": None, "answer": ""}
    assert ev._compare({"type": "min_rows", "value": 1}, actual)
    assert not ev._compare({"type": "min_rows", "value": 2}, actual)
    assert ev._compare({"type": "sparql_result", "vars": ["name"],
                        "rows": [["Alice"]]}, actual)
