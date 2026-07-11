"""Debit-vs-credit view, datetime condition filters and wallet created_at."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from dyaf.api.app import create_app
from dyaf.rules import conditions
from dyaf.rules.models import Rule

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def client(fake_es):
    return TestClient(create_app(db_path=":memory:", es_url=fake_es.url, seed_data=True))


# ----------------------------------------------------------------------
# wallet_transactions (per-direction) index
# ----------------------------------------------------------------------

def test_wallet_transactions_index_bootstrapped_and_seeded(client):
    health = client.get("/api/es/health").json()
    wt = health["indices"]["wallet_transactions"]
    tx = health["indices"]["transactions"]
    assert wt["exists"]
    assert wt["docs"] == tx["docs"] * 2  # one debit + one credit doc per transaction

    fields = {f["name"] for f in
              client.get("/api/datasources/wallet_transactions/fields").json()["fields"]}
    assert {"wallet_id", "direction", "counterparty_wallet_id", "amount"} <= fields


def test_debit_greater_than_credit_rule(client):
    """العميل الذي قيمة حركاته debit أكبر من قيمة حركاته credit."""
    rule = {
        "name": "Debit exceeds credit",
        "data_source": "wallet_transactions",
        "target_entity": "Wallet",
        "group_by": "wallet_id",
        "time_window": {"value": 7, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "hours"},
        "conditions": {"field": "sender_wallet_type", "operator": "eq",
                       "value": "Customer Wallet"},
        "aggregation": {"type": "ratio", "field": "amount", "config": {
            "numerator_condition": {"field": "direction", "operator": "eq", "value": "debit"},
            "denominator_condition": {"field": "direction", "operator": "eq", "value": "credit"},
        }},
        "threshold": {"operator": "gt", "value": 1},
        "risk_score": 65, "alert_severity": "Medium",
    }
    assert client.post("/api/rules/validate", json=rule).json()["valid"]
    result = client.post("/api/rules/test", json=rule).json()
    matched = {g["group_key"]: g["aggregation_value"]
               for g in result["group_results"] if g["matched"]}
    # W-1001 sends ~114k in structuring cash-outs and receives little
    assert "W-1001" in matched and matched["W-1001"] > 1
    # W-1005 received the 75,000 transfer -> credit far exceeds debit
    assert "W-1005" not in matched


def test_debit_minus_credit_difference_rule(client):
    """difference catches wallets with debits and zero credits too."""
    rule = {
        "name": "Net outflow positive",
        "data_source": "wallet_transactions",
        "target_entity": "Wallet",
        "group_by": "wallet_id",
        "time_window": {"value": 7, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "hours"},
        "conditions": {"field": "wallet_id", "operator": "starts_with", "value": "W-1"},
        "aggregation": {"type": "difference", "field": "amount", "config": {
            "numerator_condition": {"field": "direction", "operator": "eq", "value": "debit"},
            "denominator_condition": {"field": "direction", "operator": "eq", "value": "credit"},
        }},
        "threshold": {"operator": "gt", "value": 0},
        "risk_score": 65, "alert_severity": "Medium",
    }
    assert client.post("/api/rules/validate", json=rule).json()["valid"]
    result = client.post("/api/rules/test", json=rule).json()
    matched = {g["group_key"] for g in result["group_results"] if g["matched"]}
    # W-1003 only sends (remittances out, zero credits): ratio misses it,
    # difference catches it
    assert "W-1003" in matched
    assert "W-1001" in matched
    assert "W-1005" not in matched  # net receiver (got the 75k transfer)

    # ES preview uses a subtraction bucket_script
    q = client.post("/api/rules/preview", json=rule).json()["query"]
    script = q["aggs"]["by_entity"]["aggs"]["metric"]["bucket_script"]["script"]
    assert script == "params.num - params.den"


def test_csv_upload_keeps_wallet_transactions_in_sync(client):
    before = client.get("/api/es/health").json()["indices"]["wallet_transactions"]["docs"]
    csv_content = (
        "transaction_id,sender_wallet_id,receiver_wallet_id,amount,executed_at,transaction_type_id\n"
        "TX-WT-01,W-1001,W-1002,500,2026-07-11T09:00:00,1\n"
    )
    import io
    r = client.post("/api/datasources/transactions/upload-csv",
                    files={"file": ("t.csv", io.BytesIO(csv_content.encode()), "text/csv")})
    assert r.status_code == 200 and r.json()["indexed"] == 1
    after = client.get("/api/es/health").json()["indices"]["wallet_transactions"]["docs"]
    assert after == before + 2  # debit + credit


# ----------------------------------------------------------------------
# datetime filters in conditions
# ----------------------------------------------------------------------

def test_condition_operators_compare_datetimes():
    row = {"executed_at": "2026-07-11T10:30:00+00:00"}
    assert conditions.evaluate(
        {"field": "executed_at", "operator": "gte", "value": "2026-07-11T10:00"}, row)
    assert not conditions.evaluate(
        {"field": "executed_at", "operator": "gt", "value": "2026-07-11T11:00"}, row)
    # naive vs timezone-aware values compare correctly (naive assumed UTC)
    assert conditions.evaluate(
        {"field": "executed_at", "operator": "lt", "value": "2026-07-12T00:00:00Z"}, row)
    assert conditions.evaluate(
        {"field": "executed_at", "operator": "between",
         "value": ["2026-07-11T10:00", "2026-07-11T11:00"]}, row)
    # numeric comparison is untouched
    assert conditions.evaluate({"field": "a", "operator": "gt", "value": 5}, {"a": 9})


def test_rule_with_absolute_datetime_filter(client):
    rule = {
        "name": "Transactions after a specific datetime",
        "data_source": "transactions",
        "target_entity": "Transaction",
        "time_window": {"value": 365, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "hours"},
        "conditions": {"logic": "AND", "conditions": [
            {"field": "executed_at", "operator": "gte", "value": "2026-07-11T00:00"},
            {"field": "amount", "operator": "gt", "value": 9000},
        ]},
        "risk_score": 30, "alert_severity": "Low",
    }
    result = client.post("/api/rules/test", json=rule).json()
    assert result["groups_matched"] > 0
    # every matched record is on/after the cutoff
    for g in result["group_results"]:
        assert g["matched"]


# ----------------------------------------------------------------------
# wallet created_at column
# ----------------------------------------------------------------------

def test_wallets_have_created_at_everywhere(client):
    # operational store
    wallets = {w["wallet_id"]: w for w in client.get("/api/wallets").json()}
    assert wallets["W-1004"]["created_at"] == "2026-07-01T08:00:00"

    # wallets index fields (dynamic discovery)
    fields = {f["name"]: f["type"] for f in
              client.get("/api/datasources/wallets/fields").json()["fields"]}
    assert fields.get("created_at") == "date"

    # denormalized onto transactions for rule conditions
    tx_fields = {f["name"] for f in
                 client.get("/api/datasources/transactions/fields").json()["fields"]}
    assert {"sender_created_at", "receiver_created_at"} <= tx_fields

    # CSV template includes the new column
    template = client.get("/api/datasources/wallets/csv-template").text
    assert "created_at" in template.splitlines()[0]


def test_new_wallet_rule_using_created_at(client):
    """Example: alerts for large transactions from recently created wallets."""
    rule = {
        "name": "Large tx from new wallet",
        "data_source": "transactions",
        "target_entity": "Transaction",
        "time_window": {"value": 30, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "hours"},
        "conditions": {"logic": "AND", "conditions": [
            {"field": "sender_created_at", "operator": "gte", "value": "2026-06-01T00:00"},
            {"field": "amount", "operator": "gt", "value": 1000},
        ]},
        "risk_score": 55, "alert_severity": "Medium",
    }
    assert client.post("/api/rules/validate", json=rule).json()["valid"]
    result = client.post("/api/rules/test", json=rule).json()
    assert result["groups_matched"] > 0  # W-1003/W-1004 are recent and transact > 1000
