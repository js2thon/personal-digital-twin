from app.modules.evaluation.attribution import attribute


def test_intent_mismatch_is_f1():
    result = {
        "passed": False, "engine": "llm", "sparql": "SELECT * {}",
        "expected_intent": "count_people", "answer_text": "",
        "expected": {"type": "min_rows", "value": 1},
        "actual": {"ok": True, "kind": "SELECT", "rows": [["Alice"]],
                   "intent": "list_people"},
    }
    diagnosis = attribute(result)
    assert diagnosis["primary_code"] == "F1"
