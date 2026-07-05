"""Contract test for the expanded /analyze/idea intake — the endpoint must accept
the optional structured fields (industry/geography/customer/model/stage) AND remain
backward-compatible with a bare {"idea": "..."} body. MOCK_MODE keeps the pipeline
offline, but /analyze/idea now identifies the caller (get_current_user), so the
test creates a real user + JWT and needs Postgres for that — skips without a DB."""
import os

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-prod-min-32-bytes-long-xyz")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import connection  # noqa: E402
from app.main import app  # noqa: E402
from tests.authutil import cleanup_users, make_user  # noqa: E402

pytestmark = pytest.mark.skipif(not connection.is_enabled(),
                                reason="requires a database (set DATABASE_URL or PG*)")

client = TestClient(app)

_AUTH: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _auth():
    _AUTH.update(make_user())
    yield
    cleanup_users(_AUTH.get("user_id"))


def test_idea_accepts_full_structured_intake():
    r = client.post("/api/v1/analyze/idea", json={
        "idea": "an app for dog walkers to schedule visits and take payments",
        "industry": "pet services",
        "target_geography": "Ahmedabad, India",
        "target_customer": "B2C pet owners",
        "business_model": "subscription marketplace",
        "stage": "MVP",
    }, headers=_AUTH["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] and body["status"] == "running_discovery"


def test_idea_still_accepts_bare_body():
    r = client.post("/api/v1/analyze/idea", json={"idea": "a scheduling tool for barbers"},
                    headers=_AUTH["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running_discovery"


def test_idea_partial_context_is_allowed():
    # only some optional fields provided — the rest default to ""
    r = client.post("/api/v1/analyze/idea",
                    json={"idea": "a marketplace for local honey", "target_geography": "Gujarat"},
                    headers=_AUTH["headers"])
    assert r.status_code == 200, r.text


def test_idea_still_requires_a_non_blank_idea():
    assert client.post("/api/v1/analyze/idea", json={"industry": "fintech"},
                       headers=_AUTH["headers"]).status_code == 422
    assert client.post("/api/v1/analyze/idea", json={"idea": "   "},
                       headers=_AUTH["headers"]).status_code == 422


def test_idea_requires_authentication():
    # no bearer -> get_current_user rejects before the pipeline starts
    assert client.post("/api/v1/analyze/idea",
                       json={"idea": "a scheduling tool for barbers"}).status_code == 401
