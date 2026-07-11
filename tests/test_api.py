import pytest
from fastapi.testclient import TestClient

from dyaf.api.app import create_app


@pytest.fixture()
def client():
    return TestClient(create_app(db_path=":memory:", seed_data=True))


RULE = {
    "name": "Structuring Detection",
    "description": "Many just-below-threshold cash-outs in 24h",
    "data_source": "transactions",
    "target_entity": "Wallet",
    "group_by": "sender_wallet_id",
    "execution_frequency": {"value": 1, "unit": "hours"},
    "time_window": {"value": 24, "unit": "hours"},
    "conditions": {"logic": "AND", "conditions": [
        {"field": "amount", "operator": "between", "value": [9000, 9999]},
        {"field": "transaction_type_en", "operator": "eq", "value": "Cash Out"},
    ]},
    "aggregation": {"type": "count"},
    "threshold": {"operator": "gte", "value": 5},
    "risk_score": 90,
    "alert_severity": "Critical",
}


def test_metadata_and_dynamic_fields(client):
    meta = client.get("/api/metadata").json()
    assert "transactions" in meta["datasources"]
    assert any(a["name"] == "percentage" for a in meta["aggregations"])
    assert any(o["name"] == "between" for o in meta["operators"])

    fields = client.get("/api/datasources/transactions/fields").json()
    names = {f["name"] for f in fields["fields"]}
    # denormalized + platform fields discovered dynamically
    assert {"amount", "merchant_category", "sender_risk_rating",
            "receiver_wallet_type", "transaction_type_ar"} <= names
    assert fields["timestamp_field"] == "executed_at"

    assert client.get("/api/datasources/nope/fields").status_code == 404


def test_validate_preview_test_save_execute_flow(client):
    # validate
    r = client.post("/api/rules/validate", json=RULE).json()
    assert r["valid"], r["errors"]

    # invalid rule reports errors
    bad = dict(RULE, group_by="missing_field")
    r = client.post("/api/rules/validate", json=bad).json()
    assert not r["valid"] and any("missing_field" in e for e in r["errors"])

    # preview produces ES DSL
    r = client.post("/api/rules/preview", json=RULE).json()
    assert r["valid"] and r["query"]["aggs"]["by_entity"]["terms"]["field"] == "sender_wallet_id"

    # test (dry run): finds the structuring wallet but persists nothing
    r = client.post("/api/rules/test", json=RULE).json()
    assert r["dry_run"] is True and r["groups_matched"] == 1
    assert client.get("/api/alerts").json() == []

    # save
    r = client.post("/api/rules", json=RULE)
    assert r.status_code == 201
    rule_id = r.json()["rule_id"]
    assert r.json()["version"] == 1

    # execute -> creates the alert
    r = client.post(f"/api/rules/{rule_id}/execute").json()
    assert r["groups_matched"] == 1
    alerts = client.get("/api/alerts").json()
    assert len(alerts) == 1
    a = alerts[0]
    assert a["wallet_id"] == "W-1001"
    assert a["customer"] == "Ahmad Khalil"
    assert a["rule_name"] == "Structuring Detection"
    assert a["rule_version"] == 1
    assert a["investigation_status"] == "New"

    # investigation workflow
    r = client.put(f"/api/alerts/{a['alert_id']}/status",
                   json={"investigation_status": "In Review"}).json()
    assert r["investigation_status"] == "In Review"
    r = client.put(f"/api/alerts/{a['alert_id']}/status",
                   json={"investigation_status": "Bogus"})
    assert r.status_code == 422

    # update rule -> version bump
    updated = dict(RULE, description="v2", rule_id=rule_id)
    r = client.put(f"/api/rules/{rule_id}", json=updated).json()
    assert r["version"] == 2
    versions = client.get(f"/api/rules/{rule_id}/versions").json()
    assert [v["version"] for v in versions] == [1, 2]

    # delete
    assert client.delete(f"/api/rules/{rule_id}").json() == {"deleted": rule_id}
    assert client.get(f"/api/rules/{rule_id}").status_code == 404


def test_saving_invalid_rule_is_rejected(client):
    bad = dict(RULE, threshold={"operator": "??", "value": "x"})
    r = client.post("/api/rules", json=bad)
    assert r.status_code == 422


def test_scheduler_endpoint(client):
    client.post("/api/rules", json=RULE)
    r = client.post("/api/scheduler/run").json()
    assert r["executed"] == 1
    # immediately after, nothing is due
    r = client.post("/api/scheduler/run").json()
    assert r["executed"] == 0


def test_internal_wallet_settings_crud(client):
    iw = client.get("/api/internal-wallets").json()
    assert {"wallet_id": "W-9001", "name": "Card Settlement",
            "description": "Settlement wallet for external card scheme transactions"} in iw

    # reclassify an existing internal wallet
    r = client.post("/api/internal-wallets", json={
        "wallet_id": "W-9002", "name": "FX Settlement", "description": "Updated purpose"})
    assert r.status_code == 201
    iw = {w["wallet_id"]: w for w in client.get("/api/internal-wallets").json()}
    assert iw["W-9002"]["name"] == "FX Settlement"

    # unknown wallet id rejected
    r = client.post("/api/internal-wallets", json={"wallet_id": "W-404", "name": "x"})
    assert r.status_code == 422

    client.delete("/api/internal-wallets/W-9002")
    assert "W-9002" not in {w["wallet_id"] for w in client.get("/api/internal-wallets").json()}


def test_platform_endpoints(client):
    wallets = client.get("/api/wallets").json()
    assert any(w["wallet_type"] == "Internal Wallet" for w in wallets)
    types = client.get("/api/transaction-types").json()
    assert any(t["name_ar"] == "تحويل بين محفظتين" for t in types)
    txs = client.get("/api/transactions?limit=5").json()
    assert len(txs) == 5


def test_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Rule Builder" in r.text
