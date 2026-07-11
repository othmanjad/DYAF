"""Index bootstrap, CSV template/upload and dynamic field discovery tests."""
import io

import pytest
from fastapi.testclient import TestClient

from dyaf import ingest
from dyaf.api.app import create_app
from dyaf.datasources.elasticsearch_source import ElasticsearchDataSource


@pytest.fixture()
def client(fake_es):
    return TestClient(create_app(db_path=":memory:", es_url=fake_es.url, seed_data=True))


# ----------------------------------------------------------------------
# Index bootstrap (create on first run)
# ----------------------------------------------------------------------

def test_ensure_index_creates_once(fake_es):
    src = ElasticsearchDataSource(fake_es.url, "transactions",
                                  mappings=ingest.transactions_mappings())
    assert not src.index_exists()
    assert src.ensure_index() == {"index": "transactions", "created": True}
    assert src.index_exists()
    assert src.ensure_index() == {"index": "transactions", "created": False}
    # mapping applied -> fields discoverable before any document exists
    names = {f.name for f in src.get_fields()}
    assert {"amount", "executed_at", "sender_risk_rating", "merchant_category"} <= names


def test_app_startup_bootstraps_and_seeds(client):
    health = client.get("/api/es/health").json()
    assert health["reachable"]
    assert health["indices"]["transactions"]["exists"]
    assert health["indices"]["wallets"]["exists"]
    assert health["indices"]["transactions"]["docs"] > 100
    assert health["indices"]["wallets"]["docs"] == 9


def test_es_setup_endpoint_idempotent(client):
    r = client.post("/api/es/setup").json()
    assert r["indices"]["transactions"]["created"] is False


# ----------------------------------------------------------------------
# CSV template download
# ----------------------------------------------------------------------

def test_csv_template_download(client):
    r = client.get("/api/datasources/transactions/csv-template")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert 'attachment; filename="transactions_template.csv"' == r.headers["content-disposition"]
    header = r.text.splitlines()[0].split(",")
    assert header[:6] == ["transaction_id", "sender_wallet_id", "receiver_wallet_id",
                          "amount", "executed_at", "transaction_type_id"]
    assert len(r.text.splitlines()) == 2  # header + sample row

    r = client.get("/api/datasources/wallets/csv-template")
    assert "wallet_id" in r.text and "pep_status" in r.text

    assert client.get("/api/datasources/nope/csv-template").status_code == 404


# ----------------------------------------------------------------------
# CSV upload
# ----------------------------------------------------------------------

def _upload(client, ds, content):
    return client.post(f"/api/datasources/{ds}/upload-csv",
                       files={"file": (f"{ds}.csv", io.BytesIO(content.encode()), "text/csv")})


def test_upload_transactions_csv_indexes_and_enriches(client):
    before = client.get("/api/es/health").json()["indices"]["transactions"]["docs"]
    csv_content = (
        "transaction_id,sender_wallet_id,receiver_wallet_id,amount,executed_at,"
        "transaction_type_id,reference_number,fee,currency\n"
        "TX-CSV-01,W-1001,W-2001,9750.00,2026-07-11T09:00:00,3,REF-CSV-01,10,USD\n"
        "TX-CSV-02,W-1003,W-9002,2500.00,2026-07-11T10:00:00,5,REF-CSV-02,5,USD\n"
    )
    r = _upload(client, "transactions", csv_content)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["indexed"] == 2 and body["errors"] == []

    after = client.get("/api/es/health").json()["indices"]["transactions"]["docs"]
    assert after == before + 2

    # uploaded rows are enriched and immediately visible to rules
    test_rule = {
        "name": "csv check", "data_source": "transactions", "target_entity": "Wallet",
        "group_by": "sender_wallet_id",
        "time_window": {"value": 365, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "hours"},
        "conditions": {"field": "transaction_id", "operator": "eq", "value": "TX-CSV-01"},
        "aggregation": {"type": "count"}, "threshold": {"operator": "gte", "value": 1},
        "risk_score": 10, "alert_severity": "Low",
    }
    result = client.post("/api/rules/test", json=test_rule).json()
    assert result["groups_matched"] == 1
    assert result["group_results"][0]["group_key"] == "W-1001"


def test_upload_csv_with_new_column_becomes_rule_field(client):
    """Requirement §9: new indexed fields automatically become available."""
    fields_before = {f["name"] for f in
                     client.get("/api/datasources/transactions/fields").json()["fields"]}
    assert "device_id" not in fields_before

    csv_content = (
        "transaction_id,sender_wallet_id,receiver_wallet_id,amount,executed_at,"
        "transaction_type_id,device_id\n"
        "TX-CSV-10,W-1002,W-1005,50.00,2026-07-11T08:00:00,1,DEV-999\n"
    )
    r = _upload(client, "transactions", csv_content)
    assert r.status_code == 200 and r.json()["indexed"] == 1

    fields_after = {f["name"] for f in
                    client.get("/api/datasources/transactions/fields").json()["fields"]}
    assert "device_id" in fields_after

    # and it is immediately usable in a rule
    rule = {
        "name": "device rule", "data_source": "transactions", "target_entity": "Transaction",
        "group_by": "transaction_id",
        "time_window": {"value": 365, "unit": "days"},
        "execution_frequency": {"value": 1, "unit": "hours"},
        "conditions": {"field": "device_id", "operator": "eq", "value": "DEV-999"},
        "aggregation": {"type": "count"}, "threshold": {"operator": "gte", "value": 1},
        "risk_score": 10, "alert_severity": "Low",
    }
    assert client.post("/api/rules/validate", json=rule).json()["valid"]
    assert client.post("/api/rules/test", json=rule).json()["groups_matched"] == 1


def test_upload_wallets_csv_updates_enrichment_source(client):
    csv_content = (
        "wallet_id,owner_name,nationality,residence_country,date_of_birth,"
        "risk_rating,kyc_status,pep_status,wallet_type\n"
        "W-7777,New Customer,JO,JO,1990-01-01,Low,Verified,false,Customer Wallet\n"
    )
    r = _upload(client, "wallets", csv_content)
    assert r.status_code == 200 and r.json()["indexed"] == 1
    # wallet registered in the operational store (used for enrichment + alerts)
    wallets = {w["wallet_id"] for w in client.get("/api/wallets").json()}
    assert "W-7777" in wallets
    # and searchable in the wallets index
    assert client.get("/api/es/health").json()["indices"]["wallets"]["docs"] == 10


def test_upload_rejects_missing_required_columns(client):
    r = _upload(client, "transactions", "transaction_id,amount\nTX-1,5\n")
    assert r.status_code == 422
    assert "Missing required column" in str(r.json()["detail"])


def test_upload_reports_row_level_errors(client):
    csv_content = (
        "transaction_id,sender_wallet_id,receiver_wallet_id,amount,executed_at,transaction_type_id\n"
        "TX-OK-1,W-1001,W-1002,10,2026-07-11T08:00:00,1\n"
        ",W-1001,W-1002,20,2026-07-11T08:05:00,1\n"
    )
    r = _upload(client, "transactions", csv_content)
    assert r.status_code == 200
    body = r.json()
    assert body["indexed"] == 1
    assert any("line 3" in e for e in body["errors"])


def test_upload_unknown_datasource(client):
    r = _upload(client, "nope", "a,b\n1,2\n")
    assert r.status_code == 404
