from app.modules.evaluation.attribution import attribute


def _result(**overrides):
    value = {
        "passed": False, "engine": "llm", "sparql": "SELECT ?x WHERE {?x ?p ?o}",
        "error": None, "answer_text": "", "expected": {"type": "answer_set", "values": ["Alice"]},
        "actual": {"ok": True, "kind": "SELECT", "rows": [], "intent": "list_people"},
    }
    value.update(overrides)
    return value


def test_query_failure_is_f2_with_evidence():
    result = _result(error="SPARQL syntax error", actual={"ok": False, "rows": [], "intent": "list_people"})
    diagnosis = attribute(result)
    assert diagnosis["primary_code"] == "F2"
    assert diagnosis["confidence"] >= 0.9
    assert diagnosis["evidence"]


def test_explicit_ontology_gap_is_f3():
    result = _result(engine="guard", sparql="", answer_text="当前问题超出本体的表达力")
    diagnosis = attribute(result)
    assert diagnosis["primary_code"] == "F3"


def test_empty_result_with_positive_gold_is_f5():
    diagnosis = attribute(_result())
    assert diagnosis["primary_code"] == "F5"


def test_nonempty_but_wrong_result_is_f6():
    result = _result(actual={"ok": True, "kind": "SELECT", "rows": [["Bob"]], "intent": "list_people"})
    assert attribute(result)["primary_code"] == "F6"


def test_insufficient_evidence_remains_unknown():
    result = _result(actual={"ok": True, "kind": "SELECT", "rows": [], "intent": "list_people"},
                     expected={"type": "min_rows", "value": 0}, sparql="SELECT * {}")
    diagnosis = attribute(result)
    assert diagnosis["primary_code"] == "unknown"
    assert diagnosis["confidence"] == 0
