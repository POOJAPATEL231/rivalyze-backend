"""Persistence-first short-circuit must NOT serve an empty/degraded report.

Regression: companies analyzed by an early build (or a rate-limit-degraded run)
have a completed-but-empty report stored. Re-analyzing such a company used to
return that blank report forever (UI showed empty strategies/arrays) instead of
re-running the working pipeline. find_completed_report must skip empty reports.
"""
from app.db.repository import _MemStore

EMPTY = {"recommendations": [], "head_to_head": [], "opportunities": [],
         "swot": {"strengths": [], "weaknesses": [], "opportunities": [], "threats": []}}
FULL = {"recommendations": [{"action": "Bundle AI"}], "head_to_head": [],
        "opportunities": [], "swot": {"strengths": [], "weaknesses": [],
                                      "opportunities": [], "threats": []}}


def _completed_run(m: _MemStore, job: str, company_id: str, report: dict) -> str:
    rid = m.create_run(job, company_id)
    m.save_report(rid, report)
    m.finish_run(job)
    return rid


def test_only_empty_report_is_not_served():
    m = _MemStore()
    cid = m.create_company("Acme", "widgets")
    _completed_run(m, "job-empty", cid, EMPTY)
    # nothing populated -> fall through to a fresh run
    assert m.find_completed_report("Acme") is None


def test_populated_report_is_served_even_if_a_newer_empty_exists():
    m = _MemStore()
    cid = m.create_company("Acme", "widgets")
    _completed_run(m, "job-good", cid, FULL)
    _completed_run(m, "job-empty", cid, EMPTY)   # newer, but blank
    got = m.find_completed_report("acme")         # case-insensitive
    assert got is not None and got["job_id"] == "job-good"


def test_swot_only_report_counts_as_populated():
    m = _MemStore()
    cid = m.create_company("Beta", "")
    swot_only = dict(EMPTY, swot={"strengths": ["strong brand"], "weaknesses": [],
                                  "opportunities": [], "threats": []})
    _completed_run(m, "job-swot", cid, swot_only)
    got = m.find_completed_report("Beta")
    assert got is not None and got["job_id"] == "job-swot"
