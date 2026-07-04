"""Refresh-token flow — rotation, reuse detection, logout, expiry.

Runs on the in-memory store (no DB). Run:
  MOCK_MODE=1 python -m pytest tests/contract/test_refresh.py -q
"""
import datetime as dt
import os

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-prod-min-32-bytes-long-xyz")

from fastapi.testclient import TestClient  # noqa: E402

from app.core import refresh_store, security  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _signup(email):
    r = client.post("/api/v1/auth/signup", json={"email": email, "password": "a-good-password"})
    assert r.status_code == 201
    return r.json()


def test_signup_returns_access_and_refresh():
    body = _signup("rt.pair@example.com")
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"


def test_refresh_rotates_to_new_usable_token():
    r1 = _signup("rt.rotate@example.com")["refresh_token"]
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": r1})
    assert r.status_code == 200
    r2 = r.json()["refresh_token"]
    assert r2 != r1                                   # a NEW refresh token is issued
    assert r.json()["access_token"]                   # ...with a fresh access token
    # the new token is itself usable (rotates again). NB: replaying the OLD r1
    # is a reuse event, covered separately in test_reuse_of_rotated_token_*.
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": r2}).status_code == 200


def test_refresh_garbage_401():
    assert client.post("/api/v1/auth/refresh",
                       json={"refresh_token": "not-a-real-token"}).status_code == 401


def test_reuse_of_rotated_token_kills_the_family():
    r1 = _signup("rt.reuse@example.com")["refresh_token"]
    r2 = client.post("/api/v1/auth/refresh", json={"refresh_token": r1}).json()["refresh_token"]
    # replay the already-rotated r1 -> reuse detected
    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": r1})
    assert replay.status_code == 401
    assert replay.json()["detail"] == "refresh token reuse detected"
    # ...and the still-"valid" r2 is now revoked too (whole family killed)
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": r2}).status_code == 401


def test_logout_revokes_refresh():
    r1 = _signup("rt.logout@example.com")["refresh_token"]
    assert client.post("/api/v1/auth/logout", json={"refresh_token": r1}).status_code == 204
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": r1}).status_code == 401


def test_expired_refresh_401():
    email = "rt.expired@example.com"
    _signup(email)
    user_id = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {client.post('/api/v1/auth/login', json={'email': email, 'password': 'a-good-password'}).json()['access_token']}"},
    ).json()["user_id"]

    raw = security.generate_refresh_token()
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    refresh_store.store(user_id, security.hash_refresh_token(raw), past)

    r = client.post("/api/v1/auth/refresh", json={"refresh_token": raw})
    assert r.status_code == 401
    assert r.json()["detail"] == "refresh token expired"
