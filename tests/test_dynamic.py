"""Dynamic platform features: datasource configs, generic compare
aggregation, configurable enrichment and field-value suggestions."""
import io

import pytest
from fastapi.testclient import TestClient

from dyaf.api.app import create_app


@pytest.fixture()
def client(fake_es):
    return TestClient(create_app(db_path=":memory:", es_url=fake_es.url, seed_data=True))


# ----------------------------------------------------------------------
# Dynamic datasources
# ----------------------------------------------------------------------

CARD_EVENTS = {
    "name": "card_events",
    "timestamp_field": "event_time",
    "id_field": "event_id",
    "required_fields": ["event_id", "event_time", "card_wallet_id", "amount"],
    "enrichments": [
        {"key_field": "card_wallet_id", "lookup": "wallets", "prefix": "holder_"},
    ],
}

CARD_CSV = (
    "event_id,event_time,card_wallet_id,amount,merchant_country\n"
    "EV-1,2026-07-11T09:00:00,W-1001,150.0,JO\n"
    "EV-2,2026-07-11T10:00:00,W-1001,900.0,MT\n"
    "EV-3,2026-07-11T11:00:00,W-1002,20.0,JO\n"
)


def test_builtin_configs_seeded(client):
    cfgs = {c["name"]: c for c in client.get("/api/datasource-configs").json()}
    assert {"transactions", "wallets", "wallet_transactions"} <= set(cfgs)
    assert all(cfgs[n]["builtin"] for n in ("transactions", "wallets", "wallet_transactions"))
    # built-ins cannot be deleted or overwritten
    assert client.delete("/api/datasource-configs/transactions").status_code == 422
    assert client.post("/api/datasource-configs",
                       json={"name": "transactions"}).status_code == 422


def test_full_dynamic_datasource_lifecycle(client):
    # 1. register a brand-new datasource from the API (no code changes)
    r = client.post("/api/datasource-configs", json=CARD_EVENTS)
    assert r.status_code == 201, r.text
    assert r.json()["index_setup"]["created"] is True

    # appears everywhere immediately
    assert "card_events" in client.get("/api/metadata").json()["datasources"]
    health = client.get("/api/es/health").json()
    assert health["indices"]["card_events"]["exists"]

    # 2. CSV template derived from its config
    template = client.get("/api/datasources/card_events/csv-template")
    assert template.status_code == 200
    header = template.text.splitlines()[0]
    assert "event_id" in header and "event_time" in header

    # 3. upload data with an EXTRA column not declared anywhere
    r = client.post("/api/datasources/card_events/upload-csv",
                    files={"file": ("c.csv", io.BytesIO(CARD_CSV.encode()), "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["indexed"] == 3

    # 4. fields discovered dynamically, including the extra column and
    #    the configured enrichment output (holder_owner_name from wallets)
    fields = {f["name"] for f in
              client.get("/api/datasources/card_events/fields").json()["fields"]}
    assert {"event_id", "amount", "merchant_country", "holder_owner_name",
            "holder_risk_rating"} <= fields

    # 5. rules run against it like any built-in source
    rule = {
        "name": "High card spend", "data_source": "card_events",
        "target_entity": "Wallet", "group_by": "card_wallet_id",
        "time_window": {"value": 365, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "hours"},
        "conditions": {"field": "amount", "operator": "gt", "value": 0},
        "aggregation": {"type": "sum", "field": "amount"},
        "threshold": {"operator": "gt", "value": 1000},
        "risk_score": 50, "alert_severity": "Medium",
    }
    result = client.post("/api/rules/test", json=rule).json()
    matched = {g["group_key"] for g in result["group_results"] if g["matched"]}
    assert matched == {"W-1001"}  # 150 + 900 = 1050 > 1000

    # 6. deleting removes it from the registry (index kept)
    assert client.delete("/api/datasource-configs/card_events").status_code == 200
    assert "card_events" not in client.get("/api/metadata").json()["datasources"]
    assert client.get("/api/datasources/card_events/fields").status_code == 404


def test_dynamic_datasource_validation(client):
    assert client.post("/api/datasource-configs", json={"name": ""}).status_code == 422
    r = client.post("/api/datasource-configs", json={
        "name": "x", "enrichments": [{"key_field": "a", "lookup": "nope"}]})
    assert r.status_code == 422
    r = client.post("/api/datasource-configs", json={
        "name": "x", "enrichments": [{"lookup": "wallets"}]})
    assert r.status_code == 422


# ----------------------------------------------------------------------
# Generic compare aggregation
# ----------------------------------------------------------------------

def _compare_rule(left, right, operation, threshold):
    return {
        "name": "compare rule", "data_source": "wallet_transactions",
        "target_entity": "Wallet", "group_by": "wallet_id",
        "time_window": {"value": 7, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "hours"},
        "aggregation": {"type": "compare",
                        "config": {"left": left, "right": right, "operation": operation}},
        "threshold": threshold,
        "risk_score": 60, "alert_severity": "Medium",
    }


def test_compare_count_debit_vs_credit(client):
    rule = _compare_rule(
        {"type": "count", "condition": {"field": "direction", "operator": "eq", "value": "debit"}},
        {"type": "count", "condition": {"field": "direction", "operator": "eq", "value": "credit"}},
        "subtract", {"operator": "gt", "value": 0})
    assert client.post("/api/rules/validate", json=rule).json()["valid"]
    result = client.post("/api/rules/test", json=rule).json()
    matched = {g["group_key"] for g in result["group_results"] if g["matched"]}
    assert "W-1001" in matched          # many outgoing cash-outs
    assert "W-9001" not in matched      # settlement wallet only receives


def test_compare_mixed_aggregations_divide(client):
    # avg debit amount vs avg credit amount per wallet
    rule = _compare_rule(
        {"type": "avg", "field": "amount",
         "condition": {"field": "direction", "operator": "eq", "value": "debit"}},
        {"type": "avg", "field": "amount",
         "condition": {"field": "direction", "operator": "eq", "value": "credit"}},
        "divide", {"operator": "gt", "value": 5})
    result = client.post("/api/rules/test", json=rule).json()
    assert result["groups_evaluated"] > 0
    # W-1001: avg debit ~thousands (structuring) vs small avg credit
    w1001 = next(g for g in result["group_results"] if g["group_key"] == "W-1001")
    assert w1001["matched"]


def test_compare_preview_and_validation(client):
    rule = _compare_rule(
        {"type": "sum", "field": "amount",
         "condition": {"field": "direction", "operator": "eq", "value": "debit"}},
        {"type": "count"},
        "subtract", {"operator": "gt", "value": 0})
    q = client.post("/api/rules/preview", json=rule).json()
    assert q["valid"], q["errors"]
    aggs = q["query"]["aggs"]["by_entity"]["aggs"]
    assert "left" in aggs and "right" in aggs
    assert aggs["metric"]["bucket_script"]["script"] == "params.l - params.r"
    assert aggs["left"]["aggs"]["measure"] == {"sum": {"field": "amount"}}

    # validation errors
    bad = _compare_rule({"type": "sum"}, "not-a-dict", "multiply",
                        {"operator": "gt", "value": 0})
    errors = client.post("/api/rules/validate", json=bad).json()["errors"]
    joined = "\n".join(errors)
    assert "requires a field" in joined
    assert "config.right" in joined
    assert "subtract" in joined  # operation error mentions valid ops


# ----------------------------------------------------------------------
# Field value suggestions (dropdown source)
# ----------------------------------------------------------------------

def test_field_values_endpoint(client):
    r = client.get("/api/datasources/wallet_transactions/fields/direction/values").json()
    assert sorted(r["values"]) == ["credit", "debit"]

    r = client.get("/api/datasources/transactions/fields/currency/values").json()
    assert "USD" in r["values"]

    r = client.get("/api/datasources/transactions/fields/merchant_category/values").json()
    assert "Gambling" in r["values"] and "Grocery" in r["values"]

    assert client.get("/api/datasources/nope/fields/x/values").status_code == 404
