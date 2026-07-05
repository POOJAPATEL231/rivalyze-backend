"""Two-phase contract tests (TC-C09..C14) — analyze → gate → confirm → completed,
plus the read endpoints (reports / evidence / history / export).

DB-backed (runs persist to Postgres); skips if no database is configured. Under
the TestClient, BackgroundTasks run synchronously after each response, so a run
reaches its next resting state (awaiting_confirmation, then completed) by the time
the POST returns — polling is defensive.

Run:  MOCK_MODE=1 python -m pytest tests/contract/test_two_phase.py -q
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


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    with connection.pool().connection() as c, c.cursor() as cur:
        cur.execute("DELETE FROM companies WHERE slug LIKE 'twophaseco%'")
        c.commit()


def _fresh() -> str:
    return f"TwoPhaseCo{uuid.uuid4().hex[:8]}"


def _poll(job_id: str, wanted: set[str], tries: int = 300) -> dict:
    # generous ceiling (30s): the shared Azure PG can be slow under a full-suite
    # run; a short poll window made this file flaky at suite level.
    last: dict = {}
    for _ in range(tries):
        last = client.get(f"/api/v1/runs/{job_id}").json()
        if last.get("status") in wanted:
            return last
        time.sleep(0.1)
    return last


def _to_gate(company: str):
    r = client.post("/api/v1/analyze", json={"company": company, "domain": "testing"})
    assert r.status_code == 200
    assert r.json()["status"] == "running_discovery"       # TC-C09: phase-1 status
    job = r.json()["job_id"]
    s = _poll(job, {"awaiting_confirmation", "completed", "failed"})
    assert s["status"] == "awaiting_confirmation", s
    return job, s["result"]["competitors"]


# ------------------------------- TC-C09 -------------------------------
def test_analyze_parks_at_awaiting_confirmation():
    company = _fresh()
    job, comps = _to_gate(company)
    assert comps, "gate should propose competitors"
    names = [c["name"].lower() for c in comps]
    assert company.lower() not in names                    # self-exclusion


# ------------------------------- TC-C10 -------------------------------
def test_confirm_runs_analysis_and_serves_report_and_reads():
    company = _fresh()
    job, comps = _to_gate(company)
    edited = comps[:1]                                     # user keeps only the first rival
    kept_name = edited[0]["name"]

    r = client.post(f"/api/v1/runs/{job}/confirm", json={"confirmed_competitors": edited})
    assert r.status_code == 202
    assert r.json()["status"] == "confirmed"

    s = _poll(job, {"completed", "failed"})
    assert s["status"] == "completed"
    run_id = s["run_id"]
    assert run_id

    report = client.get(f"/api/v1/reports/{run_id}").json()
    h2h_rivals = {r for row in report["head_to_head"] for r in row["rivals"]}
    assert h2h_rivals <= {kept_name}                       # only the confirmed rival analyzed

    # evidence: unknown claim_ref -> valid 200 empty; unknown run -> 404
    ev = client.get("/api/v1/evidence/nope:none", params={"run_id": run_id})
    assert ev.status_code == 200 and ev.json()["sources"] == []
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get("/api/v1/evidence/x:y", params={"run_id": missing}).status_code == 404

    # history includes this completed run (served by history_routes.py)
    hist = client.get("/api/v1/history", params={"company": company}).json()
    assert any(h["job_id"] == job for h in hist)

    # markdown export (served by history_routes.py; unsupported format -> 400)
    ex = client.get(f"/api/v1/reports/{run_id}/export", params={"format": "md"})
    assert ex.status_code == 200
    assert ex.headers["content-type"].startswith("text/markdown")
    assert ex.text.lstrip().startswith("#")               # a markdown report
    assert client.get(f"/api/v1/reports/{run_id}/export",
                      params={"format": "pdf"}).status_code == 400


# ---------------------------- TC-C11/C12 -----------------------------
def test_confirm_unknown_job_is_404():
    r = client.post("/api/v1/runs/rivalyze-nope-000000/confirm",
                    json={"confirmed_competitors": [{"name": "X"}]})
    assert r.status_code == 404


def test_double_confirm_is_409_and_never_double_runs():
    company = _fresh()
    job, comps = _to_gate(company)
    first = client.post(f"/api/v1/runs/{job}/confirm", json={"confirmed_competitors": comps[:1]})
    assert first.status_code == 202
    # the run has already moved past awaiting_confirmation -> the CAS rejects a second confirm
    second = client.post(f"/api/v1/runs/{job}/confirm", json={"confirmed_competitors": comps[:1]})
    assert second.status_code == 409
    s = _poll(job, {"completed", "failed"})
    assert s["status"] == "completed"
    # confirm on a completed run is likewise 409
    assert client.post(f"/api/v1/runs/{job}/confirm",
                       json={"confirmed_competitors": comps[:1]}).status_code == 409


# ------------------------------- TC-C13 -------------------------------
def test_confirm_empty_list_is_422():
    company = _fresh()
    job, _ = _to_gate(company)
    r = client.post(f"/api/v1/runs/{job}/confirm", json={"confirmed_competitors": []})
    assert r.status_code == 422


# ------------------------------- TC-C14 -------------------------------
def test_confirm_without_token_is_401(monkeypatch):
    from app.core import config
    # with a static token configured, an unauthenticated request is rejected before
    # the handler runs (auth dependency fires first).
    monkeypatch.setattr(config, "BEARER_TOKEN", "secret-token-for-this-test")
    r = client.post("/api/v1/runs/whatever/confirm",
                    json={"confirmed_competitors": [{"name": "X"}]})
    assert r.status_code == 401
