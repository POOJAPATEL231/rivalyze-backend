"""Contract test for the expanded /analyze/idea intake — the endpoint must accept
the optional structured fields (industry/geography/customer/model/stage) AND remain
backward-compatible with a bare {"idea": "..."} body. Runs in-memory (no DB): the
POST only starts Phase 1, so it needs no Postgres. MOCK_MODE keeps it offline and
opens require_token."""
import os

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-prod-min-32-bytes-long-xyz")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_idea_accepts_full_structured_intake():
    r = client.post("/api/v1/analyze/idea", json={
        "idea": "an app for dog walkers to schedule visits and take payments",
        "industry": "pet services",
        "target_geography": "Ahmedabad, India",
        "target_customer": "B2C pet owners",
        "business_model": "subscription marketplace",
        "stage": "MVP",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] and body["status"] == "running_discovery"


def test_idea_still_accepts_bare_body():
    r = client.post("/api/v1/analyze/idea", json={"idea": "a scheduling tool for barbers"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running_discovery"


def test_idea_partial_context_is_allowed():
    # only some optional fields provided — the rest default to ""
    r = client.post("/api/v1/analyze/idea",
                    json={"idea": "a marketplace for local honey", "target_geography": "Gujarat"})
    assert r.status_code == 200, r.text


def test_idea_still_requires_a_non_blank_idea():
    assert client.post("/api/v1/analyze/idea", json={"industry": "fintech"}).status_code == 422
    assert client.post("/api/v1/analyze/idea", json={"idea": "   "}).status_code == 422
