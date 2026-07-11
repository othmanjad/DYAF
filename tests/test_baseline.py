"""Per-entity baseline capabilities: relative time filters (now-Xd),
dormant-reactivation compare operation, and field-based thresholds
(K × declared/expected value). Closes TM-05/06/07-style scenarios."""
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from dyaf.api.app import create_app
from dyaf.rules import conditions


@pytest.fixture()
def client(fake_es):
    return TestClient(create_app(db_path=":memory:", es_url=fake_es.url, seed_data=True))


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ----------------------------------------------------------------------
# Relative date math in conditions
# ----------------------------------------------------------------------

def test_relative_date_math_in_conditions():
    recent = {"executed_at": _iso(2)}       # 2 hours ago
    old = {"executed_at": _iso(24 * 10)}    # 10 days ago
    cond = {"field": "executed_at", "operator": "gte", "value": "now-24h"}
    assert conditions.evaluate(cond, recent)
    assert not conditions.evaluate(cond, old)

    cond = {"field": "executed_at", "operator": "lt", "value": "now-7d"}
    assert conditions.evaluate(cond, old)
    assert not conditions.evaluate(cond, recent)

    # plain "now": everything in the past matches lt now
    assert conditions.evaluate({"field": "executed_at", "operator": "lt", "value": "now"}, old)
    # unknown token is not silently treated as a date
    assert not conditions.evaluate(
        {"field": "executed_at", "operator": "gte", "value": "yesterday"}, recent)


def test_relative_dates_flow_through_es_fetch(client):
    """A rule whose condition uses now-24h only sees recent transactions."""
    rule = {
        "name": "Recent activity only", "data_source": "transactions",
        "target_entity": "Transaction",
        "time_window": {"value": 30, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "hours"},
        "conditions": {"logic": "AND", "conditions": [
            {"field": "executed_at", "operator": "gte", "value": "now-24h"},
            {"field": "amount", "operator": "gt", "value": 0},
        ]},
        "risk_score": 10, "alert_severity": "Low",
    }
    day = client.post("/api/rules/test", json=rule).json()
    rule["conditions"]["conditions"][0]["value"] = "now-30d"
    month = client.post("/api/rules/test", json=rule).json()
    assert 0 < day["rows_evaluated"] < month["rows_evaluated"]


# ----------------------------------------------------------------------
# Dormant account reactivation (TM-07)
# ----------------------------------------------------------------------

DORMANT_CSV_HEADER = ("transaction_id,sender_wallet_id,receiver_wallet_id,amount,"
                      "executed_at,transaction_type_id\n")


def _dormancy_rule():
    return {
        "name": "Dormant Account Reactivation",
        "data_source": "wallet_transactions",
        "target_entity": "Wallet", "group_by": "wallet_id",
        "time_window": {"value": 180, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "days"},
        "conditions": {"field": "direction", "operator": "eq", "value": "debit"},
        "aggregation": {"type": "compare", "config": {
            # seeded history spans ~7 days, so use a 3-day dormancy boundary:
            # "active in the last 3 days with NO activity in the prior 177"
            "left": {"type": "count",
                     "condition": {"field": "executed_at", "operator": "gte", "value": "now-3d"}},
            "right": {"type": "count",
                      "condition": {"field": "executed_at", "operator": "lt", "value": "now-3d"}},
            "operation": "left_when_right_zero",
        }},
        "threshold": {"operator": "gte", "value": 2},
        "risk_score": 75, "alert_severity": "High",
    }


def test_dormant_reactivation_rule(client):
    # W-8888 was dormant for months, then suddenly transacts twice this week
    csv_content = DORMANT_CSV_HEADER + (
        f"TX-DORM-1,W-8888,W-2001,900,{_iso(20)},3\n"
        f"TX-DORM-2,W-8888,W-2001,850,{_iso(40)},3\n"
    )
    # register the wallet first so enrichment resolves
    wallet_csv = ("wallet_id,owner_name,wallet_type\n"
                  "W-8888,Dormant Customer,Customer Wallet\n")
    client.post("/api/datasources/wallets/upload-csv",
                files={"file": ("w.csv", io.BytesIO(wallet_csv.encode()), "text/csv")})
    r = client.post("/api/datasources/transactions/upload-csv",
                    files={"file": ("t.csv", io.BytesIO(csv_content.encode()), "text/csv")})
    assert r.status_code == 200 and r.json()["indexed"] == 2

    rule = _dormancy_rule()
    assert client.post("/api/rules/validate", json=rule).json()["valid"]
    result = client.post("/api/rules/test", json=rule).json()
    by_key = {g["group_key"]: g for g in result["group_results"]}

    # dormant wallet fires: 2 recent debits, zero older ones
    assert by_key["W-8888"]["matched"]
    assert by_key["W-8888"]["aggregation_value"] == 2
    # continuously active seeded wallet does NOT fire (older activity exists)
    assert not by_key["W-1001"]["matched"]
    assert by_key["W-1001"]["aggregation_value"] == 0

    # ES preview compiles the guard into a conditional bucket_script
    q = client.post("/api/rules/preview", json=rule).json()["query"]
    script = q["aggs"]["by_entity"]["aggs"]["metric"]["bucket_script"]["script"]
    assert script == "params.r == 0 ? params.l : 0"


# ----------------------------------------------------------------------
# Per-entity (field-based) thresholds — K × declared value (TM-05)
# ----------------------------------------------------------------------

def test_threshold_against_declared_activity(client):
    # custom wallet attribute uploaded via CSV: expected weekly volume
    wallet_csv = (
        "wallet_id,owner_name,wallet_type,expected_weekly_volume\n"
        "W-1001,Ahmad Khalil,Customer Wallet,1000\n"     # spends ~114k -> way above 3x1000
        "W-1002,Layla Hassan,Customer Wallet,100000\n"   # 75k+ but below 3x100000
    )
    r = client.post("/api/datasources/wallets/upload-csv",
                    files={"file": ("w.csv", io.BytesIO(wallet_csv.encode()), "text/csv")})
    assert r.status_code == 200

    # custom attribute is now a rule field on wallets
    wallet_fields = {f["name"] for f in
                     client.get("/api/datasources/wallets/fields").json()["fields"]}
    assert "expected_weekly_volume" in wallet_fields

    # re-upload one transaction per wallet so fresh docs carry the new
    # denormalized sender_expected_weekly_volume attribute
    tx_csv = DORMANT_CSV_HEADER + (
        f"TX-BASE-1,W-1001,W-2001,9500,{_iso(3)},3\n"
        f"TX-BASE-2,W-1002,W-1005,75000,{_iso(4)},1\n"
    )
    client.post("/api/datasources/transactions/upload-csv",
                files={"file": ("t.csv", io.BytesIO(tx_csv.encode()), "text/csv")})

    rule = {
        "name": "Volume exceeds 3x declared",
        "data_source": "transactions",
        "target_entity": "Wallet", "group_by": "sender_wallet_id",
        "time_window": {"value": 30, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "days"},
        "conditions": {"field": "sender_expected_weekly_volume", "operator": "exists"},
        "aggregation": {"type": "sum", "field": "amount"},
        "threshold": {"operator": "gt", "value_field": "sender_expected_weekly_volume",
                      "multiplier": 3},
        "risk_score": 70, "alert_severity": "High",
    }
    assert client.post("/api/rules/validate", json=rule).json()["valid"], \
        client.post("/api/rules/validate", json=rule).json()["errors"]
    result = client.post("/api/rules/test", json=rule).json()
    by_key = {g["group_key"]: g for g in result["group_results"]}
    assert by_key["W-1001"]["matched"]        # 9,500 > 3 × 1,000
    assert not by_key["W-1002"]["matched"]    # 75,000 < 3 × 100,000

    # alert records the resolved per-entity threshold
    rule_id = client.post("/api/rules", json=rule).json()["rule_id"]
    client.post(f"/api/rules/{rule_id}/execute")
    alert = client.get("/api/alerts", params={"rule_id": rule_id}).json()[0]
    assert alert["rule_result"]["threshold"]["value"] == 3000.0
    assert alert["rule_result"]["configured_threshold"]["value_field"] == \
        "sender_expected_weekly_volume"


def test_field_threshold_validation(client):
    rule = {
        "name": "bad", "data_source": "transactions", "target_entity": "Wallet",
        "group_by": "sender_wallet_id",
        "time_window": {"value": 7, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "days"},
        "aggregation": {"type": "sum", "field": "amount"},
        "threshold": {"operator": "gt", "value_field": "no_such_field", "multiplier": 0},
        "risk_score": 50, "alert_severity": "Medium",
    }
    errors = client.post("/api/rules/validate", json=rule).json()["errors"]
    joined = "\n".join(errors)
    assert "no_such_field" in joined
    assert "multiplier" in joined


def test_entities_without_baseline_are_skipped(client):
    """Wallets lacking the declared-value attribute don't produce alerts."""
    # only W-1005 declares an expected volume
    wallet_csv = ("wallet_id,owner_name,wallet_type,expected_weekly_volume\n"
                  "W-1005,Khaled Odeh,Customer Wallet,50\n")
    client.post("/api/datasources/wallets/upload-csv",
                files={"file": ("w.csv", io.BytesIO(wallet_csv.encode()), "text/csv")})
    tx_csv = DORMANT_CSV_HEADER + f"TX-SKIP-1,W-1005,W-2001,200,{_iso(2)},1\n"
    client.post("/api/datasources/transactions/upload-csv",
                files={"file": ("t.csv", io.BytesIO(tx_csv.encode()), "text/csv")})

    rule = {
        "name": "3x declared", "data_source": "transactions",
        "target_entity": "Wallet", "group_by": "sender_wallet_id",
        "time_window": {"value": 30, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "days"},
        "aggregation": {"type": "sum", "field": "amount"},
        "threshold": {"operator": "gt", "value_field": "sender_expected_weekly_volume",
                      "multiplier": 3},
        "risk_score": 70, "alert_severity": "High",
    }
    result = client.post("/api/rules/test", json=rule).json()
    by_key = {g["group_key"]: g for g in result["group_results"]}
    # W-1005 has a baseline (150) and easily exceeds it
    assert by_key["W-1005"]["matched"]
    # wallets with no declared value are skipped entirely, not falsely alerted
    assert "W-1001" not in by_key
    assert result["groups_evaluated"] > len(by_key)
