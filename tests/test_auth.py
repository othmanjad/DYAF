"""Elasticsearch authentication tests (username/password + API key)."""
import pytest
from fastapi.testclient import TestClient

from dyaf import ingest
from dyaf.api.app import create_app, es_config_from_env, load_dotenv
from dyaf.datasources.elasticsearch_source import (ElasticsearchDataSource,
                                                   ElasticsearchError)
from dyaf.testing.fake_es import FakeElasticsearch


@pytest.fixture()
def secured_es():
    server = FakeElasticsearch(username="elastic", password="s3cret").start()
    yield server
    server.stop()


def test_wrong_or_missing_credentials_rejected(secured_es):
    no_creds = ElasticsearchDataSource(secured_es.url, "transactions")
    conn = no_creds.check_connection()
    assert conn["reachable"] and not conn["authenticated"]
    assert not no_creds.ping()
    with pytest.raises(ElasticsearchError):
        no_creds.create_index()

    bad = ElasticsearchDataSource(secured_es.url, "transactions",
                                  username="elastic", password="wrong")
    assert not bad.ping()


def test_basic_auth_full_flow(secured_es):
    src = ElasticsearchDataSource(secured_es.url, "transactions",
                                  username="elastic", password="s3cret",
                                  mappings=ingest.transactions_mappings())
    assert src.auth_mode == "basic"
    assert src.ping()
    assert src.ensure_index()["created"] is True
    result = src.bulk_index([{"transaction_id": "TX-1", "amount": 10.0,
                              "executed_at": "2026-07-11T10:00:00"}],
                            id_field="transaction_id")
    assert result["indexed"] == 1
    assert src.count() == 1
    assert {f.name for f in src.get_fields()} >= {"amount", "executed_at"}
    assert len(src.fetch()) == 1


def test_app_with_credentials(secured_es):
    app = create_app(db_path=":memory:", es_url=secured_es.url, seed_data=True,
                     es_auth={"username": "elastic", "password": "s3cret"})
    client = TestClient(app)
    health = client.get("/api/es/health").json()
    assert health["reachable"] and health["authenticated"]
    assert health["auth_mode"] == "basic"
    assert health["indices"]["transactions"]["docs"] > 100


def test_app_without_credentials_reports_auth_failure(secured_es):
    app = create_app(db_path=":memory:", es_url=secured_es.url, seed_data=True,
                     es_auth={})
    client = TestClient(app)
    health = client.get("/api/es/health").json()
    assert health["reachable"] and not health["authenticated"]
    assert health["indices"] == {}
    assert client.get("/api/datasources/transactions/fields").status_code == 503


def test_api_key_header():
    src = ElasticsearchDataSource("http://example:9200", "transactions",
                                  api_key="abc123==")
    assert src.auth_mode == "api_key"
    assert src.http.headers["Authorization"] == "ApiKey abc123=="
    # API key wins over username/password when both are provided
    both = ElasticsearchDataSource("http://example:9200", "transactions",
                                   api_key="k", username="u", password="p")
    assert both.auth_mode == "api_key"


def test_env_and_dotenv_loading(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("ELASTICSEARCH_URL", "ELASTICSEARCH_USERNAME",
                "ELASTICSEARCH_PASSWORD", "ELASTICSEARCH_VERIFY_CERTS"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(
        "ELASTICSEARCH_URL=https://es.example:9200\n"
        "ELASTICSEARCH_USERNAME=elastic\n"
        "ELASTICSEARCH_PASSWORD='p@ss'\n"
        "# comment line\n"
        "ELASTICSEARCH_VERIFY_CERTS=false\n")
    load_dotenv(str(tmp_path / ".env"))
    cfg = es_config_from_env()
    assert cfg["es_url"] == "https://es.example:9200"
    assert cfg["username"] == "elastic"
    assert cfg["password"] == "p@ss"
    assert cfg["verify_certs"] is False

    # real environment variables take precedence over .env values
    monkeypatch.setenv("ELASTICSEARCH_USERNAME", "svc-dyaf")
    load_dotenv(str(tmp_path / ".env"))
    assert es_config_from_env()["username"] == "svc-dyaf"
