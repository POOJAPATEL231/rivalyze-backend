"""Route tests for GET /api/v1/history and GET /api/v1/reports/{run_id}/export.

Runs against the real ASGI app + real Postgres — skips cleanly when no
DATABASE_URL/PG* is configured (same pattern as tests/security/test_security.py).

Run:  MOCK_MODE=1 pytest tests/contract/test_history_export.py -q
"""
import os
import uuid

os.environ.setdefault("MOCK_MODE", "1")  # must be set before app import

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import connection, repository  # noqa: E402
from app.main import app  # noqa: E402
from tests.authutil import cleanup_users, make_user  # noqa: E402

client = TestClient(app)

requires_db = pytest.mark.skipif(
    not connection.is_enabled(),
    reason="requires a database (set DATABASE_URL or PG*)",
)

_MINIMAL_REPORT_FIELDS = {
    "swot": {"strengths": [], "weaknesses": [], "opportunities": [], "threats": []},
    "sentiment": {},
    "head_to_head": [],
    "opportunities": [],
    "recommendations": [],
    "low_signal_findings": [],
    "analysis_date": "2026-01-01T00:00:00+00:00",
}


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def completed_run():
    # /history now identifies the caller and returns only THEIR runs, so the
    # fixture owns the run with a real user and exposes that user's auth headers.
    owner = make_user()
    name = _unique("HistoryExportCo")
    company_id = repository.create_company(name, domain="test")
    job_id = _unique("job")
    run_id = repository.create_run(job_id, company_id, owner["user_id"])
    repository.finish_run(job_id, "HIGH", 0.77)
    yield {"job_id": job_id, "run_id": run_id, "company": name, "company_id": company_id,
           "user_id": owner["user_id"], "headers": owner["headers"]}
    with repository.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM companies WHERE id = %s", (company_id,))
    cleanup_users(owner["user_id"])


# ================================ /history ================================
@requires_db
def test_history_returns_completed_run(completed_run):
    r = client.get("/api/v1/history", params={"company": completed_run["company"]},
                   headers=completed_run["headers"])
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["job_id"] == completed_run["job_id"]
    assert entry["company"] == completed_run["company"]
    assert entry["threat_level"] == "HIGH"
    assert entry["confidence"] == pytest.approx(0.77)
    assert entry["created_at"]


@requires_db
def test_history_filter_excludes_other_companies(completed_run):
    r = client.get("/api/v1/history", params={"company": "zz-no-such-company-zz"},
                   headers=completed_run["headers"])
    assert r.status_code == 200
    assert r.json() == []


@requires_db
def test_history_requires_authentication():
    # no bearer -> get_current_user rejects before any data is read
    assert client.get("/api/v1/history").status_code == 401


@requires_db
def test_history_hides_other_users_runs(completed_run):
    # a different, valid user must not see completed_run's row
    other = make_user()
    try:
        r = client.get("/api/v1/history", params={"company": completed_run["company"]},
                       headers=other["headers"])
        assert r.status_code == 200
        assert r.json() == []
    finally:
        cleanup_users(other["user_id"])


@requires_db
def test_history_newest_first(completed_run):
    job2 = _unique("job")
    repository.create_run(job2, completed_run["company_id"], completed_run["user_id"])
    repository.finish_run(job2, "LOW", 0.2)

    r = client.get("/api/v1/history", params={"company": completed_run["company"]},
                   headers=completed_run["headers"])
    job_ids = [e["job_id"] for e in r.json()]
    assert job_ids.index(job2) < job_ids.index(completed_run["job_id"])


# ================================ /export ==================================
@requires_db
def test_export_unknown_run_id_404():
    r = client.get(f"/api/v1/reports/{uuid.uuid4()}/export", params={"format": "md"})
    assert r.status_code == 404


@requires_db
def test_export_unsupported_format_400(completed_run):
    repository.save_report(completed_run["run_id"], {
        "company": completed_run["company"], "threat_level": "HIGH",
        "executive_summary": "", **_MINIMAL_REPORT_FIELDS,
    })
    r = client.get(f"/api/v1/reports/{completed_run['run_id']}/export", params={"format": "pdf"})
    assert r.status_code == 400


@requires_db
def test_export_returns_markdown_attachment_and_caches_it(completed_run):
    report = {
        "company": completed_run["company"], "threat_level": "HIGH",
        "executive_summary": "Test summary.",
        "swot": {"strengths": ["x"], "weaknesses": [], "opportunities": [], "threats": []},
        "sentiment": {}, "head_to_head": [],
        "opportunities": [{"text": "Do X", "evidence_ids": [], "claim_ref": "opp:x"}],
        "recommendations": [], "low_signal_findings": [],
        "analysis_date": "2026-01-01T00:00:00+00:00",
    }
    repository.save_report(completed_run["run_id"], report)

    r = client.get(f"/api/v1/reports/{completed_run['run_id']}/export", params={"format": "md"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"]
    assert "Test summary." in r.text
    assert "Do X" in r.text

    cached = repository.get_report(completed_run["run_id"])
    assert cached["md_export"]

    r2 = client.get(f"/api/v1/reports/{completed_run['run_id']}/export", params={"format": "md"})
    assert r2.text == r.text
