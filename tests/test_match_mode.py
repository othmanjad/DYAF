"""Match-mode rules: no aggregation / group_by / threshold required.

Example use case: alert on every transaction above 1,000 JOD.
"""
import pytest
from fastapi.testclient import TestClient

from dyaf.api.app import create_app
from dyaf.rules.models import Rule, validate_rule
from dyaf.rules.query_builder import build_rule_query


@pytest.fixture()
def client(fake_es):
    return TestClient(create_app(db_path=":memory:", es_url=fake_es.url, seed_data=True))


MATCH_RULE = {
    "name": "Large Transaction Over 1000",
    "description": "Alert on every transaction above 1,000",
    "data_source": "transactions",
    "target_entity": "Transaction",
    "execution_frequency": {"value": 1, "unit": "hours"},
    "time_window": {"value": 7, "unit": "days"},
    "conditions": {"field": "amount", "operator": "gt", "value": 1000},
    "risk_score": 40,
    "alert_severity": "Medium",
    # no group_by, no aggregation, no threshold
}


def test_match_rule_is_valid_without_group_by_aggregation_threshold(client):
    r = client.post("/api/rules/validate", json=MATCH_RULE).json()
    assert r["valid"], r["errors"]


def test_match_rule_requires_conditions(client):
    bad = {k: v for k, v in MATCH_RULE.items() if k != "conditions"}
    r = client.post("/api/rules/validate", json=bad).json()
    assert not r["valid"]
    assert any("at least one condition" in e for e in r["errors"])


def test_aggregation_rules_still_require_group_by():
    defn = dict(MATCH_RULE, aggregation={"type": "count"},
                threshold={"operator": "gte", "value": 5})
    errors = validate_rule(defn)
    assert any("group_by field is required for aggregation rules" in e for e in errors)


def test_match_rule_generates_one_alert_per_matching_transaction(client):
    # seeded data: structuring txs (9000-9900) + one 75,000 transfer + others
    r = client.post("/api/rules/test", json=MATCH_RULE).json()
    assert r["dry_run"] is True
    assert r["groups_matched"] == r["groups_evaluated"] == r["rows_evaluated"]
    assert r["groups_matched"] >= 13  # 12 structuring + 1 large transfer

    # every group is a single record keyed by its transaction id
    g = r["group_results"][0]
    assert g["row_count"] == 1
    assert g["group_key"].startswith("TX-")

    # save + execute -> persisted alerts, each tied to one transaction
    rule_id = client.post("/api/rules", json=MATCH_RULE).json()["rule_id"]
    client.post(f"/api/rules/{rule_id}/execute")
    alerts = client.get("/api/alerts", params={"rule_id": rule_id}).json()
    assert len(alerts) == r["groups_matched"]
    a = alerts[0]
    assert len(a["transaction_ids"]) == 1
    assert a["rule_result"]["mode"] == "match"
    assert a["rule_result"]["threshold"] is None
    assert a["rule_result"]["record"]["amount"] > 1000
    assert a["customer"] is not None and a["wallet_id"] is not None


def test_match_rule_with_group_by_alerts_per_entity(client):
    rule = dict(MATCH_RULE, name="Wallets with any large tx", group_by="sender_wallet_id",
                target_entity="Wallet")
    r = client.post("/api/rules/test", json=rule).json()
    # several transactions collapse into per-wallet groups
    assert r["groups_matched"] < r["rows_evaluated"]
    keys = {g["group_key"] for g in r["group_results"] if g["matched"]}
    assert "W-1001" in keys and "W-1002" in keys


def test_match_rule_duplicate_suppression(client):
    rule_id = client.post("/api/rules", json=MATCH_RULE).json()["rule_id"]
    client.post(f"/api/rules/{rule_id}/execute")
    n1 = len(client.get("/api/alerts", params={"rule_id": rule_id}).json())
    client.post(f"/api/rules/{rule_id}/execute")
    n2 = len(client.get("/api/alerts", params={"rule_id": rule_id}).json())
    assert n1 == n2  # same records do not raise duplicate open alerts


def test_match_rule_query_preview_has_no_aggs(client):
    r = client.post("/api/rules/preview", json=MATCH_RULE).json()
    assert r["valid"], r["errors"]
    assert "aggs" not in r["query"]
    assert r["query"]["size"] == 10000
    musts = r["query"]["query"]["bool"]["must"]
    assert {"range": {"amount": {"gt": 1000}}} in musts


def test_engine_direct_match_mode(fake_es, client):
    # Rule object built programmatically (API-independent path)
    app_state = client.app.state
    rule = Rule(
        name="Over 1000", data_source="transactions", target_entity="Transaction",
        time_window={"value": 7, "unit": "days"},
        conditions={"field": "amount", "operator": "gt", "value": 1000},
        risk_score=40, alert_severity="Medium",
    )
    result = app_state.engine.execute(rule, dry_run=True)
    assert result.groups_matched >= 13
    assert all(g.row_count == 1 for g in result.group_results)
