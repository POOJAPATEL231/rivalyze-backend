"""Contract smoke test — analyze → poll → the awaiting_confirmation GATE.

Two-phase pipeline: /analyze runs discovery only and parks at
awaiting_confirmation (the full analyze → confirm → completed path lives in
tests/contract/test_two_phase.py). DB-backed; unique company per run, cascade
delete on teardown; skips if no DB.

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
from tests.authutil import cleanup_users, make_user  # noqa: E402

pytestmark = pytest.mark.skipif(not connection.is_enabled(),
                                reason="requires a database (set DATABASE_URL or PG*)")

client = TestClient(app)
TAG = uuid.uuid4().hex[:8]
COMPANY = f"SmokeCo{TAG}"          # unique per run; slug = lower(COMPANY)

# /analyze now identifies the caller (get_current_user); one module user
# authenticates every analyze call. _AUTH["headers"] carries its JWT.
_AUTH: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    _AUTH.update(make_user())
    yield
    # deleting the company cascades to its runs + competitors
    with connection.pool().connection() as c, c.cursor() as cur:
        cur.execute("DELETE FROM companies WHERE slug = %s", (COMPANY.lower(),))
        c.commit()
    cleanup_users(_AUTH.get("user_id"))


def test_health_is_open_and_shaped():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "rivalyze"}


def test_analyze_then_poll_reaches_gate_and_persists():
    r = client.post("/api/v1/analyze", json={"company": COMPANY, "domain": "testing"},
                    headers=_AUTH["headers"])
    assert r.status_code == 200
    assert r.json()["status"] == "running_discovery"       # phase-1 status
    job_id = r.json()["job_id"]

    status = {}
    for _ in range(100):
        status = client.get(f"/api/v1/runs/{job_id}").json()
        if status["status"] in ("awaiting_confirmation", "completed", "failed"):
            break
        time.sleep(0.1)

    assert status["status"] == "awaiting_confirmation"      # parks at the gate
    names = [c["name"].lower() for c in status["result"]["competitors"]]
    assert names, "expected at least one proposed competitor"
    assert COMPANY.lower() not in names                     # self-exclusion

    # prove the proposal is actually in Postgres, not just the response
    with connection.pool().connection() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM runs WHERE job_id = %s", (job_id,))
        assert cur.fetchone()[0] == "awaiting_confirmation"
        cur.execute("SELECT r.id FROM runs r WHERE r.job_id = %s", (job_id,))
        run_id = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM competitors WHERE run_id = %s", (run_id,))
        assert cur.fetchone()[0] == len(names)


def test_unknown_job_returns_404():
    assert client.get("/api/v1/runs/does-not-exist").status_code == 404


def test_empty_request_returns_422():
    r = client.post("/api/v1/analyze", json={"company": "", "idea": None},
                    headers=_AUTH["headers"])
    assert r.status_code == 422
    assert r.json()["detail"] == "provide a company or an idea"
