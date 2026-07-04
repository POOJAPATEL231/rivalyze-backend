"""Auth contract + security tests — signup/login/me over JWT.

Covers the happy path plus the security-relevant edges: duplicate signup,
wrong password, unknown email (generic 401, no enumeration), weak password
rejected (422), and /me gated on a valid token.

Run:  MOCK_MODE=1 python -m pytest tests/contract/test_auth.py -q
"""
import os

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-prod-min-32-bytes-long-xyz")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

GOOD = {"email": "Alice@Example.com", "password": "correct horse battery"}


def _signup(email="new.user@example.com", password="s3cretpassword"):
    return client.post("/api/v1/auth/signup", json={"email": email, "password": password})


def test_signup_returns_token_201():
    r = _signup("signup.ok@example.com")
    assert r.status_code == 201
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


def test_signup_never_leaks_password():
    r = _signup("no.leak@example.com", "supersecretpw")
    assert "supersecretpw" not in r.text
    assert "password" not in r.json()


def test_duplicate_signup_409_case_insensitive():
    _signup("dupe@example.com")
    r = _signup("DUPE@example.com")  # same account, different casing
    assert r.status_code == 409


def test_weak_password_rejected_422():
    r = client.post("/api/v1/auth/signup", json={"email": "weak@example.com", "password": "short"})
    assert r.status_code == 422


def test_login_success_and_me_roundtrip():
    email = "roundtrip@example.com"
    _signup(email, "averylongpassword")
    r = client.post("/api/v1/auth/login", json={"email": email, "password": "averylongpassword"})
    assert r.status_code == 200
    token = r.json()["access_token"]

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email
    assert me.json()["user_id"]


def test_login_wrong_password_401_generic():
    email = "wrongpw@example.com"
    _signup(email, "rightpassword1")
    r = client.post("/api/v1/auth/login", json={"email": email, "password": "wrongpassword1"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid email or password"


def test_login_unknown_email_same_error_no_enumeration():
    r = client.post("/api/v1/auth/login",
                    json={"email": "ghost@example.com", "password": "whateverpass"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid email or password"


def test_me_requires_token():
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_rejects_garbage_token():
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401
