"""Regression tests for the lifecycle audit fixes:
 - a completed, report-bearing run is never flipped to 'failed' by post-completion
   bookkeeping (A1.2);
 - a degraded run still persists an honest report shell so /reports never 404s (A1.4);
 - the poll tells '0 rivals found' apart from 'still discovering' (A1.1).
These use the in-memory repository (no DATABASE_URL in the test env)."""
from app.core import lifecycle
from app.db import repository
from app.db.repository import _MemStore
from app.models import AnalyzeRequest, CompetitiveReport


def test_degraded_report_shell_is_valid_and_carries_findings():
    shell = lifecycle._degraded_report_shell("Acme", {"low_signal_findings": ["thin corpus"]})
    rep = CompetitiveReport.model_validate(shell)          # must be a valid report
    assert rep.company == "Acme"
    assert rep.executive_summary.strip()
    assert "thin corpus" in rep.low_signal_findings


def test_poll_distinguishes_zero_rivals_from_in_progress():
    cid = repository.create_company("PollCo", "x")
    repository.create_run("poll-job-1", cid)
    repository.update_run_status("poll-job-1", "running_discovery", "discovery")
    assert lifecycle.get_run("poll-job-1").result is None          # still discovering
    repository.update_run_status("poll-job-1", "awaiting_confirmation", "awaiting_confirmation")
    res = lifecycle.get_run("poll-job-1").result
    assert res is not None and res.competitors == []               # done, 0 found


def test_completed_run_not_flipped_to_failed_by_bookkeeping(monkeypatch):
    cid = repository.create_company("BookCo", "x")
    rid = repository.create_run("book-job-1", cid)

    def boom(*a, **k):
        raise RuntimeError("db blip on lane-stats write")

    monkeypatch.setattr(lifecycle, "_persist_lane_stats", boom)
    confirmed = [{"name": "Rival One", "category": "direct", "rationale": "overlap"}]
    lifecycle.start_analysis("book-job-1", rid, confirmed)         # MOCK agents

    row = repository.get_run("book-job-1")
    assert row["status"] == "completed"                            # NOT failed
    assert repository.get_report(rid) is not None                  # report persisted


def test_set_run_company_updates_the_name():
    m = _MemStore()
    cid = m.create_company("an app for dog walkers to schedule and take payment", "")
    rid = m.create_run("j", cid)
    m.set_run_company(rid, "PackWalk", "pet services")
    assert m.get_run_company(rid)["name"] == "PackWalk"


def test_idea_mode_persists_resolved_company_not_raw_idea():
    idea_text = "an app that helps dog walkers schedule visits and take payments online"
    cid = repository.create_company(idea_text, "")     # phase 1 stores the raw idea as name
    rid = repository.create_run("idea-job-1", cid)
    req = AnalyzeRequest(company="", domain="", idea=idea_text)
    lifecycle.start_discovery("idea-job-1", rid, req)  # MOCK resolves + persists
    name = repository.get_run_company(rid)["name"]
    assert name and name != idea_text                  # the report won't be stamped with the raw idea
