"""Repository tests — exercised against the REAL Postgres (Azure), never mocked.

Every write is scoped to a uniquely-slugged pytest company; the `company`
fixture deletes that row on teardown, which cascades to its runs/reports/
competitors/signals/evidence. search_cache rows don't hang off a company, so
those tests clean up their own key explicitly.

Run:  pytest tests/db/test_repository.py -q     (needs DATABASE_URL in .env)
"""
import uuid

import pytest
from psycopg.rows import dict_row

from app.core import config
from app.db import repository as repo

if not config.DATABASE_URL:
    pytest.skip("DATABASE_URL not configured — repository tests need a real DB", allow_module_level=True)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def company():
    name = _unique("PytestCo")
    company_id = repo.create_company(name, domain="pytest fixture")
    yield {"id": company_id, "name": name}
    with repo.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM companies WHERE id = %s", (company_id,))


@pytest.fixture
def run(company):
    job_id = _unique("pytest-job")
    run_id = repo.create_run(job_id, company["id"])
    yield {"job_id": job_id, "run_id": run_id, "company": company}


# ============================== companies ===============================
def test_create_company_upserts_on_slug(company):
    again = repo.create_company(company["name"], domain="a different domain")
    assert again == company["id"]


# ================================= runs ==================================
def test_create_run_defaults(run):
    row = repo.get_run(run["job_id"])
    assert row is not None
    assert row["status"] == "queued"
    assert row["current_stage"] == "queued"
    assert row["events"] == []
    assert row["lane_stats"] == {}


def test_update_run_status(run):
    repo.update_run_status(run["job_id"], "running", "discovery")
    row = repo.get_run(run["job_id"])
    assert row["status"] == "running"
    assert row["current_stage"] == "discovery"


def test_append_events_accumulates_without_overwrite(run):
    repo.append_events(run["job_id"], [{"t": 0.1, "agent": "system", "msg": "start"}])
    repo.append_events(run["job_id"], [{"t": 0.5, "agent": "router", "msg": "gemini ok"}])
    row = repo.get_run(run["job_id"])
    assert [e["msg"] for e in row["events"]] == ["start", "gemini ok"]


def test_set_lane_stats(run):
    repo.set_lane_stats(run["job_id"], {"gemini": 3, "searches": 5})
    row = repo.get_run(run["job_id"])
    assert row["lane_stats"] == {"gemini": 3, "searches": 5}


def test_finish_run(run):
    repo.finish_run(run["job_id"], "HIGH", 0.83)
    row = repo.get_run(run["job_id"])
    assert row["status"] == "completed"
    assert row["current_stage"] == "done"
    assert row["threat_level"] == "HIGH"
    assert float(row["report_confidence"]) == pytest.approx(0.83)
    assert row["finished_at"] is not None


# ================================ reports ================================
def test_save_and_get_report_roundtrip(run):
    report = {"company": run["company"]["name"], "threat_level": "HIGH", "recommendations": []}
    report_id = repo.save_report(run["run_id"], report, md="# Title")
    assert report_id
    fetched = repo.get_report(run["run_id"])
    assert fetched["report"] == report
    assert fetched["md_export"] == "# Title"


def test_save_report_upserts_on_run_id(run):
    repo.save_report(run["run_id"], {"v": 1})
    repo.save_report(run["run_id"], {"v": 2}, md="v2")
    fetched = repo.get_report(run["run_id"])
    assert fetched["report"] == {"v": 2}
    assert fetched["md_export"] == "v2"


# ============================== competitors ==============================
def test_save_competitors(run):
    repo.save_competitors(run["run_id"], [
        {"name": "Coda", "category": "direct", "rationale": "docs+db"},
        {"name": "Airtable", "category": "indirect", "rationale": "no-code db"},
    ])
    with repo.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, category FROM competitors WHERE run_id = %s ORDER BY name",
                (run["run_id"],),
            )
            rows = cur.fetchall()
    assert rows == [("Airtable", "indirect"), ("Coda", "direct")]


# ================================ signals =================================
def test_save_signal_roundtrip(run):
    sig_id = repo.save_signal({
        "run_id": run["run_id"], "agent": "news", "competitor": "Coda",
        "type": "launch", "payload": {"event": "Coda AI launch"}, "evidence_ids": ["ev-1"],
    })
    assert sig_id
    with repo.get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT payload, evidence_ids FROM signals WHERE id = %s", (sig_id,))
            row = cur.fetchone()
    assert row["payload"] == {"event": "Coda AI launch"}
    assert row["evidence_ids"] == ["ev-1"]


# ================================ evidence =================================
def test_save_and_get_evidence(run):
    repo.save_evidence({
        "id": f"ev-{uuid.uuid4().hex[:8]}", "run_id": run["run_id"], "claim_ref": "rec:bundle-ai",
        "source_type": "news", "source_name": "TechCrunch", "url": "https://example.com/a",
        "snippet": "Coda launched an AI feature.", "source_date": "2026-01-01", "agent": "news",
    })
    repo.save_evidence({
        "id": f"ev-{uuid.uuid4().hex[:8]}", "run_id": run["run_id"], "claim_ref": "rec:bundle-ai",
        "source_type": "review", "source_name": "G2", "url": "https://example.com/b",
        "snippet": "Users like it.", "source_date": "2026-01-02", "agent": "review",
    })
    rows = repo.get_evidence(run["run_id"], "rec:bundle-ai")
    assert len(rows) == 2
    assert {r["source_name"] for r in rows} == {"TechCrunch", "G2"}


def test_save_evidence_is_idempotent_on_id(run):
    row = {
        "id": f"ev-{uuid.uuid4().hex[:8]}", "run_id": run["run_id"], "claim_ref": "pricing:x",
        "source_type": "pricing", "source_name": "Vendor", "url": "https://example.com/c",
        "snippet": "Pricing changed.", "source_date": "", "agent": "product",
    }
    repo.save_evidence(row)
    repo.save_evidence(row)  # retried write must not raise a PK violation
    rows = repo.get_evidence(run["run_id"], "pricing:x")
    assert len(rows) == 1


# ============================ persistence-first ============================
def test_find_completed_report(run):
    assert repo.find_completed_report(run["company"]["name"]) is None
    repo.finish_run(run["job_id"], "MEDIUM", 0.5)
    found = repo.find_completed_report(run["company"]["name"].upper())  # case-insensitive
    assert found == {"job_id": run["job_id"], "run_id": run["run_id"]}


def test_get_history_filters_and_orders_newest_first(company):
    job_a = _unique("pytest-job")
    run_a = repo.create_run(job_a, company["id"])
    repo.finish_run(job_a, "LOW", 0.2)

    job_b = _unique("pytest-job")
    run_b = repo.create_run(job_b, company["id"])
    repo.finish_run(job_b, "HIGH", 0.9)

    history = repo.get_history(limit=20, company=company["name"])
    job_ids = [h["job_id"] for h in history]
    assert job_ids.index(job_b) < job_ids.index(job_a)  # newest (job_b) first

    filtered = repo.get_history(limit=20, company="zz-no-such-company-zz")
    assert filtered == []


# ============================== search cache ==============================
def test_search_cache_roundtrip_and_overwrite():
    key = _unique("pytest-cache")
    try:
        assert repo.get_search_cache(key) is None
        repo.save_search_cache(key, {"results": [1, 2, 3]})
        assert repo.get_search_cache(key) == {"results": [1, 2, 3]}
        repo.save_search_cache(key, {"results": [4]})
        assert repo.get_search_cache(key) == {"results": [4]}
    finally:
        with repo.get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM search_cache WHERE key = %s", (key,))
