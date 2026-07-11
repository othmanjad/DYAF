import pytest

from dyaf.testing.fake_es import FakeElasticsearch


@pytest.fixture()
def fake_es():
    server = FakeElasticsearch().start()
    yield server
    server.stop()
