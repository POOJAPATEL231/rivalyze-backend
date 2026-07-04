"""Refresh-token flow — rotation, reuse detection, logout, expiry — against Postgres.

DB-only (no in-memory fallback). Unique email tag per run, cleaned up on
teardown; skips if no database is configured.

Run:  python -m pytest tests/contract/test_refresh.py -q
"""
import datetime as dt
import os
import uuid

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-prod-min-32-bytes-long-xyz")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import refresh_store, security  # noqa: E402
from app.db import connection  # noqa: E402
from app.main import app  # noqa: E402

pytestmark = pytest.mark.skipif(not connection.is_enabled(),
                                reason="requires a database (set DATABASE_URL or PG*)")

client = TestClient(app)
TAG = uuid.uuid4().hex[:10]


def _email(name: str) -> str:
    return f"{name}.{TAG}@example.com"


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    with connection.pool().connection() as c, c.cursor() as cur:
        cur.execute("DELETE FROM users WHERE email LIKE %s", (f"%.{TAG}@example.com",))
        c.commit()


def _signup(name):
    r = client.post("/api/v1/auth/signup",
                    json={"email": _email(name), "password": "a-good-password"})
    assert r.status_code == 201
    return r.json()


def test_signup_returns_access_and_refresh():
    body = _signup("rt-pair")
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"


def test_refresh_rotates_to_new_usable_token():
    r1 = _signup("rt-rotate")["refresh_token"]
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": r1})
    assert r.status_code == 200
    r2 = r.json()["refresh_token"]
    assert r2 != r1
    assert r.json()["access_token"]
    # replaying the OLD r1 is a reuse event, covered in test_reuse_*.
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": r2}).status_code == 200


def test_refresh_garbage_401():
    assert client.post("/api/v1/auth/refresh",
                       json={"refresh_token": "not-a-real-token"}).status_code == 401


def test_reuse_of_rotated_token_kills_the_family():
    r1 = _signup("rt-reuse")["refresh_token"]
    r2 = client.post("/api/v1/auth/refresh", json={"refresh_token": r1}).json()["refresh_token"]
    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": r1})
    assert replay.status_code == 401
    assert replay.json()["detail"] == "refresh token reuse detected"
    # the still-"valid" r2 is now revoked too (whole family killed)
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": r2}).status_code == 401


def test_logout_revokes_refresh():
    r1 = _signup("rt-logout")["refresh_token"]
    assert client.post("/api/v1/auth/logout", json={"refresh_token": r1}).status_code == 204
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": r1}).status_code == 401


def test_expired_refresh_401():
    email = _email("rt-expired")
    client.post("/api/v1/auth/signup", json={"email": email, "password": "a-good-password"})
    login = client.post("/api/v1/auth/login", json={"email": email, "password": "a-good-password"})
    user_id = client.get("/api/v1/auth/me",
                         headers={"Authorization": f"Bearer {login.json()['access_token']}"}).json()["user_id"]

    raw = security.generate_refresh_token()
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    refresh_store.store(user_id, security.hash_refresh_token(raw), past)

    r = client.post("/api/v1/auth/refresh", json={"refresh_token": raw})
    assert r.status_code == 401
    assert r.json()["detail"] == "refresh token expired"
