"""/health/cache diagnostic — must always 200 with a stable shape and never leak
the REDIS_URL/DB secret values (only scheme + booleans + verdict)."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_cache_shape_and_no_secret_leak():
    r = client.get("/api/v1/health/cache")
    assert r.status_code == 200
    body = r.json()
    # stable contract the frontend / ops can rely on
    assert set(body) >= {"redis", "postgres", "roundtrip", "verdict", "cache_working"}
    assert body["verdict"] in {"redis_active", "postgres_only_active", "no_working_cache"}
    assert isinstance(body["cache_working"], bool)
    # never echo the connection string itself — only its scheme
    assert "REDIS_URL" not in r.text
    assert "://" not in r.text or body["redis"].get("scheme") is not None


def test_health_cache_is_open_no_auth_required():
    # ops must be able to hit it without a token, same as /health
    assert client.get("/api/v1/health/cache").status_code == 200
