"""Contract smoke test — analyze → poll → completed, now against Postgres.

Runs are DB-backed (no in-memory JOBS), so this needs a database. It uses a
unique company per run and deletes it (cascade) on teardown; skips if no DB.

Run:  MOCK_MODE=1 python -m pytest tests/contract/test_smoke.py -q
"""
import os
import uuid

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-prod-min-32-bytes-long-xyz")

import time  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import connection  # noqa: E402
from app.main import app  # noqa: E402

pytestmark = pytest.mark.skipif(not connection.is_enabled(),
                                reason="requires a database (set DATABASE_URL or PG*)")

client = TestClient(app)
TAG = uuid.uuid4().hex[:8]
COMPANY = f"SmokeCo{TAG}"          # unique per run; slug = lower(COMPANY)


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    # deleting the company cascades to its runs + competitors
    with connection.pool().connection() as c, c.cursor() as cur:
        cur.execute("DELETE FROM companies WHERE slug = %s", (COMPANY.lower(),))
        c.commit()


def test_health_is_open_and_shaped():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "rivalyze"
    assert isinstance(body["counters"], dict)  # per-provider daily credit counts


def test_analyze_then_poll_completes_and_persists():
    r = client.post("/api/v1/analyze", json={"company": COMPANY, "domain": "testing"})
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    job_id = r.json()["job_id"]

    status = {}
    for _ in range(100):
        status = client.get(f"/api/v1/runs/{job_id}").json()
        if status["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)

    assert status["status"] == "completed"
    assert status["run_id"]                       # the persisted run uuid
    names = [c["name"].lower() for c in status["result"]["competitors"]]
    assert names, "expected at least one competitor"
    assert COMPANY.lower() not in names           # self-exclusion

    # prove it's actually in Postgres, not just the response
    with connection.pool().connection() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM runs WHERE job_id = %s", (job_id,))
        assert cur.fetchone()[0] == "completed"
        cur.execute("SELECT count(*) FROM competitors WHERE run_id = %s::uuid", (status["run_id"],))
        assert cur.fetchone()[0] == len(names)


def test_persistence_first_returns_existing_completed():
    # second analyze of the same company short-circuits to the completed run
    r = client.post("/api/v1/analyze", json={"company": COMPANY, "domain": "testing"})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"


def test_unknown_job_returns_404():
    assert client.get("/api/v1/runs/does-not-exist").status_code == 404


def test_empty_request_returns_422():
    r = client.post("/api/v1/analyze", json={"company": "", "idea": None})
    assert r.status_code == 422
    assert r.json()["detail"] == "provide a company or an idea"
