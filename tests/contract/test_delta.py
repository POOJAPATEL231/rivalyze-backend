"""Route tests for GET /api/v1/companies/{slug}/delta (Monitor Delta v0).

QA cases TC-S01..TC-S05 from Rivalyze_Monitor_Delta_Backend.md. Runs against
the real ASGI app + real Postgres — skips cleanly when no DATABASE_URL/PG* is
configured (same pattern as tests/contract/test_history_export.py).

Run:  MOCK_MODE=1 pytest tests/contract/test_delta.py -q
"""
import os
import uuid

os.environ.setdefault("MOCK_MODE", "1")  # must be set before app import

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import config  # noqa: E402
from app.db import connection, repository  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

requires_db = pytest.mark.skipif(
    not connection.is_enabled(),
    reason="requires a database (set DATABASE_URL or PG*)",
)


def _unique(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


# Two baseline signals present in both runs; re-found with different
# punctuation/casing in R1 on purpose — the dedupe MUST still match them.
_BASE_R0 = [
    {"agent": "news", "competitor": "ClickUp", "type": "funding",
     "payload": {"event": "ClickUp raises $100M Series C"}, "evidence_ids": []},
    {"agent": "product", "competitor": "Coda", "type": "launch",
     "payload": {"event": "Coda ships AI blocks"}, "evidence_ids": []},
]
_BASE_R1 = [
    {"agent": "news", "competitor": "ClickUp", "type": "funding",
     "payload": {"event": "CLICKUP Raises $100m, Series C!"}, "evidence_ids": []},
    {"agent": "product", "competitor": "Coda", "type": "launch",
     "payload": {"event": "Coda Ships: AI Blocks."}, "evidence_ids": []},
]
_EXTRA = {"agent": "review", "competitor": "ClickUp", "type": "pricing",
          "payload": {"event": "New enterprise pricing tier"}, "evidence_ids": ["ev-delta1"]}


def _seed_run(company_id: str, signals: list[dict], *, backdate_days: int = 0) -> str:
    """completed run + its signals; optionally backdated so latest/previous
    ordering is deterministic (finish_run stamps now(), back-to-back runs tie)."""
    job_id = _unique("job-")
    run_id = repository.create_run(job_id, company_id)
    for sig in signals:
        repository.save_signal({**sig, "run_id": run_id})
    repository.finish_run(job_id)
    if backdate_days:
        with repository.get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE runs SET finished_at = now() - %s * interval '1 day' "
                        "WHERE id = %s::uuid", (backdate_days, run_id))
            conn.commit()
    return run_id


@pytest.fixture
def company():
    name = _unique("DeltaCo")           # slug-safe: slug == name.lower()
    company_id = repository.create_company(name, domain="test")
    yield {"id": company_id, "name": name, "slug": name.lower()}
    with repository.get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM companies WHERE id = %s", (company_id,))
        conn.commit()


# ------------------------- TC-S01: dedupe proof -------------------------
@requires_db
def test_identical_runs_zero_delta(company):
    _seed_run(company["id"], _BASE_R0, backdate_days=7)
    _seed_run(company["id"], _BASE_R1)

    r = client.get(f"/api/v1/companies/{company['slug']}/delta")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0 and body["new_signals"] == []
    assert body["company"] == company["name"]
    assert body["since"]                       # R0.finished_at, ISO string
    assert "first_run" not in body             # exclude_none drops it


# ---------------------- TC-S02: seeded new signal -----------------------
@requires_db
def test_new_signal_appears_in_delta(company):
    _seed_run(company["id"], _BASE_R0, backdate_days=7)
    _seed_run(company["id"], _BASE_R1 + [_EXTRA])

    body = client.get(f"/api/v1/companies/{company['slug']}/delta").json()
    assert body["count"] == 1
    sig = body["new_signals"][0]
    assert sig == {"agent": "review", "competitor": "ClickUp", "type": "pricing",
                   "headline": "New enterprise pricing tier",
                   "evidence_ids": ["ev-delta1"], "claim_ref": "pricing:clickup"}


# ------------------------ TC-S03: first/only run ------------------------
@requires_db
def test_single_run_is_first_run(company):
    _seed_run(company["id"], _BASE_R0)
    body = client.get(f"/api/v1/companies/{company['slug']}/delta").json()
    assert body == {"count": 0, "new_signals": [], "first_run": True}


@requires_db
def test_zero_completed_runs_is_first_run(company):
    body = client.get(f"/api/v1/companies/{company['slug']}/delta").json()
    assert body == {"count": 0, "new_signals": [], "first_run": True}


# ------------------------- TC-S04: unknown slug -------------------------
@requires_db
def test_unknown_slug_404():
    r = client.get(f"/api/v1/companies/no-such-slug-{uuid.uuid4().hex}/delta")
    assert r.status_code == 404


# ------------------------- TC-S05: auth required ------------------------
def test_delta_requires_token_401(monkeypatch):
    # with a static token configured, the auth dependency fires before the
    # handler — no DB needed for the 401 half.
    monkeypatch.setattr(config, "BEARER_TOKEN", "s3cret-token")
    assert client.get("/api/v1/companies/anything/delta").status_code == 401


@requires_db
def test_delta_accepts_good_token(company, monkeypatch):
    monkeypatch.setattr(config, "BEARER_TOKEN", "s3cret-token")
    r = client.get(f"/api/v1/companies/{company['slug']}/delta",
                   headers={"Authorization": "Bearer s3cret-token"})
    assert r.status_code == 200


# ---------------- has_new flag on GET /history (popup trigger) ----------------
def _history_rows(company_name: str) -> list[dict]:
    r = client.get("/api/v1/history", params={"company": company_name})
    assert r.status_code == 200
    return r.json()


@requires_db
def test_history_flags_new_changes_on_newest_row_only(company):
    _seed_run(company["id"], _BASE_R0, backdate_days=7)
    _seed_run(company["id"], _BASE_R1 + [_EXTRA])   # latest run has 1 genuinely new signal

    rows = _history_rows(company["name"])
    assert len(rows) == 2
    assert rows[0]["has_new"] is True               # newest row -> popup
    assert rows[1]["has_new"] is False              # older row never flagged


@requires_db
def test_history_no_flag_when_delta_is_empty(company):
    _seed_run(company["id"], _BASE_R0, backdate_days=7)
    _seed_run(company["id"], _BASE_R1)              # identical (reworded) -> no delta

    rows = _history_rows(company["name"])
    assert [r["has_new"] for r in rows] == [False, False]


@requires_db
def test_history_first_run_company_not_flagged(company):
    _seed_run(company["id"], _BASE_R0)
    rows = _history_rows(company["name"])
    assert len(rows) == 1 and rows[0]["has_new"] is False
