import pytest

from dyaf.rules import aggregations

ROWS = [
    {"amount": 100, "merchant_category": "Gambling", "executed_at": "2026-07-01T10:00:00"},
    {"amount": 200, "merchant_category": "Gambling", "executed_at": "2026-07-01T11:00:00"},
    {"amount": 300, "merchant_category": "Grocery", "executed_at": "2026-07-01T12:00:00"},
    {"amount": 400, "merchant_category": "Grocery", "executed_at": "2026-07-01T13:00:00"},
]


def test_basic_aggregations():
    assert aggregations.compute("count", ROWS) == 4
    assert aggregations.compute("sum", ROWS, field="amount") == 1000
    assert aggregations.compute("avg", ROWS, field="amount") == 250
    assert aggregations.compute("min", ROWS, field="amount") == 100
    assert aggregations.compute("max", ROWS, field="amount") == 400
    assert aggregations.compute("distinct_count", ROWS, field="merchant_category") == 2


def test_percentage_by_count_and_by_sum():
    cfg = {"numerator_condition": {"field": "merchant_category", "operator": "eq", "value": "Gambling"}}
    assert aggregations.compute("percentage", ROWS, config=cfg) == 50.0  # 2 of 4 rows
    assert aggregations.compute("percentage", ROWS, field="amount", config=cfg) == 30.0  # 300/1000


def test_ratio():
    cfg = {
        "numerator_condition": {"field": "merchant_category", "operator": "eq", "value": "Gambling"},
        "denominator_condition": {"field": "merchant_category", "operator": "eq", "value": "Grocery"},
    }
    assert aggregations.compute("ratio", ROWS, field="amount", config=cfg) == pytest.approx(300 / 700)


def test_ratio_zero_denominator_is_safe():
    cfg = {
        "numerator_condition": {"field": "amount", "operator": "gt", "value": 0},
        "denominator_condition": {"field": "amount", "operator": "gt", "value": 99999},
    }
    assert aggregations.compute("ratio", ROWS, config=cfg) == 0.0


def test_stddev_and_moving_average():
    assert aggregations.compute("stddev", ROWS, field="amount") == pytest.approx(111.803, rel=1e-3)
    ma = aggregations.compute("moving_average", ROWS, field="amount", config={"window_size": 2})
    assert ma == 350  # avg of last two by time: 300, 400


def test_unknown_aggregation_raises():
    with pytest.raises(ValueError):
        aggregations.compute("nope", ROWS)


def test_registry_is_extensible():
    aggregations.register("always_42", lambda rows, field, cfg: 42.0, description="test plug-in")
    assert aggregations.compute("always_42", ROWS) == 42.0
    assert any(a["name"] == "always_42" for a in aggregations.available())
