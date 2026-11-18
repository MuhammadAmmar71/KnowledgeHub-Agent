"""
API key and debug-endpoint gating (spec section 50). The lifespan (database,
scheduler) never starts here: TestClient is used without its context
manager, and every request below is rejected before a handler would touch
the database.
"""
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.main as main


@pytest.fixture
def client_with(settings, monkeypatch):
    def make(**overrides):
        for name, value in overrides.items():
            setattr(settings, name, value)
        monkeypatch.setattr(main, "get_settings", lambda: settings)
        return TestClient(main.app)
    return make


def test_health_needs_no_key(client_with):
    client = client_with(api_key=SecretStr("k"))
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("path", ["/status", "/metrics", "/documents", "/sync/latest", "/health/graph"])
def test_protected_endpoints_reject_missing_or_wrong_key(client_with, path):
    client = client_with(api_key=SecretStr("right-key"))
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_mutating_endpoints_reject_missing_key(client_with):
    client = client_with(api_key=SecretStr("right-key"))
    for path in ("/sync", "/sync/reconcile", "/sync/pages", "/embeddings/run"):
        assert client.post(path).status_code == 401


def test_debug_endpoints_are_hidden_unless_enabled(client_with):
    client = client_with(api_key=None, enable_debug_endpoints=False)
    assert client.get("/discovery/preview").status_code == 404
    assert client.get("/debug/files/abc/preview").status_code == 404


def test_debug_endpoints_still_require_the_key_when_enabled(client_with):
    client = client_with(api_key=SecretStr("k"), enable_debug_endpoints=True)
    assert client.get("/debug/files/abc/preview").status_code == 401
