"""Auth contract + security tests — signup/login/me over JWT, against Postgres.

Auth is now DB-only (no in-memory fallback), so these run against the real
database. Each run uses a unique email tag and deletes its rows on teardown;
the whole module skips if no database is configured.

Run:  python -m pytest tests/contract/test_auth.py -q   (needs DATABASE_URL / PG* + .env)
"""
import os
import uuid

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-prod-min-32-bytes-long-xyz")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import connection  # noqa: E402  (importing app.* loads .env)
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


def _signup(name, password="s3cretpassword"):
    return client.post("/api/v1/auth/signup", json={"email": _email(name), "password": password})


def test_signup_returns_token_201():
    r = _signup("signup-ok")
    assert r.status_code == 201
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]


def test_signup_never_leaks_password():
    r = _signup("no-leak", "supersecretpw")
    assert "supersecretpw" not in r.text
    assert "password" not in r.json()


def test_duplicate_signup_409_case_insensitive():
    _signup("dupe")
    r = client.post("/api/v1/auth/signup",
                    json={"email": _email("dupe").upper(), "password": "s3cretpassword"})
    assert r.status_code == 409


def test_create_user_duplicate_raises_domain_error_not_500():
    # exercise the DB-constraint path directly (skips the route's pre-check),
    # simulating the concurrent-signup race: the 2nd INSERT must map to
    # EmailAlreadyExistsError, which the route turns into a 409 — never a 500.
    from app.core import security, user_store

    email = _email("race")
    user_store.create_user(email, security.hash_password("whatever12"))
    with pytest.raises(user_store.EmailAlreadyExistsError):
        user_store.create_user(email, security.hash_password("whatever12"))


def test_weak_password_rejected_422():
    r = client.post("/api/v1/auth/signup", json={"email": _email("weak"), "password": "short"})
    assert r.status_code == 422


def test_login_success_and_me_roundtrip():
    email = _email("roundtrip")
    client.post("/api/v1/auth/signup", json={"email": email, "password": "averylongpassword"})
    r = client.post("/api/v1/auth/login", json={"email": email, "password": "averylongpassword"})
    assert r.status_code == 200
    token = r.json()["access_token"]

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email
    assert me.json()["user_id"]


def test_login_wrong_password_401_generic():
    email = _email("wrongpw")
    client.post("/api/v1/auth/signup", json={"email": email, "password": "rightpassword1"})
    r = client.post("/api/v1/auth/login", json={"email": email, "password": "wrongpassword1"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid email or password"


def test_login_unknown_email_same_error_no_enumeration():
    r = client.post("/api/v1/auth/login",
                    json={"email": _email("ghost"), "password": "whateverpass"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid email or password"


def test_me_requires_token():
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_rejects_garbage_token():
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401
