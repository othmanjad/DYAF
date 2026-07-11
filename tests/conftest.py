import pytest
from fastapi.testclient import TestClient

from dyaf.api import create_app
from dyaf.fake_es import FakeElasticsearch


@pytest.fixture()
def fake_es():
    server = FakeElasticsearch().start()
    yield server
    server.stop()


@pytest.fixture()
def client(fake_es):
    return TestClient(create_app(db_path=":memory:", es_url=fake_es.url, seed=True))
