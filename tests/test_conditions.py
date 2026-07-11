from dyaf.rules import conditions


ROW = {"amount": 9500, "currency": "USD", "merchant_category": "Gambling",
       "sender_risk_rating": "High", "merchant_id": None}


def test_leaf_operators():
    assert conditions.evaluate({"field": "amount", "operator": "gt", "value": 9000}, ROW)
    assert not conditions.evaluate({"field": "amount", "operator": "gt", "value": 10000}, ROW)
    assert conditions.evaluate({"field": "amount", "operator": "between", "value": [9000, 10000]}, ROW)
    assert conditions.evaluate({"field": "currency", "operator": "eq", "value": "USD"}, ROW)
    assert conditions.evaluate({"field": "currency", "operator": "in", "value": ["USD", "EUR"]}, ROW)
    assert conditions.evaluate({"field": "merchant_category", "operator": "contains", "value": "gamb"}, ROW)
    assert conditions.evaluate({"field": "merchant_id", "operator": "missing"}, ROW)
    assert not conditions.evaluate({"field": "merchant_id", "operator": "exists"}, ROW)


def test_numeric_comparison_with_string_values():
    assert conditions.evaluate({"field": "amount", "operator": "eq", "value": "9500"}, ROW)


def test_nested_and_or_not():
    tree = {"logic": "AND", "conditions": [
        {"field": "amount", "operator": "gte", "value": 9000},
        {"logic": "OR", "conditions": [
            {"field": "currency", "operator": "eq", "value": "EUR"},
            {"field": "sender_risk_rating", "operator": "eq", "value": "High"},
        ]},
        {"logic": "NOT", "conditions": [
            {"field": "merchant_category", "operator": "eq", "value": "Grocery"},
        ]},
    ]}
    assert conditions.evaluate(tree, ROW)
    ROW2 = dict(ROW, sender_risk_rating="Low")
    assert not conditions.evaluate(tree, ROW2)


def test_empty_and_none_trees_match_everything():
    assert conditions.evaluate(None, ROW)
    assert conditions.evaluate({"logic": "AND", "conditions": []}, ROW)


def test_validation_reports_errors():
    errs = conditions.validate({"logic": "XOR", "conditions": [
        {"field": "nope", "operator": "wat", "value": 1},
        {"field": "amount", "operator": "gt"},  # missing value
    ]}, known_fields={"amount"})
    joined = "\n".join(errs)
    assert "invalid logic" in joined
    assert "unknown field 'nope'" in joined
    assert "unknown operator 'wat'" in joined
    assert "requires a value" in joined


def test_validation_ok():
    tree = {"logic": "AND", "conditions": [{"field": "amount", "operator": "gt", "value": 1}]}
    assert conditions.validate(tree, known_fields={"amount"}) == []
