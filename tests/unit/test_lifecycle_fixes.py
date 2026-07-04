"""Regression tests for the lifecycle audit fixes:
 - a completed, report-bearing run is never flipped to 'failed' by post-completion
   bookkeeping (A1.2);
 - a degraded run still persists an honest report shell so /reports never 404s (A1.4);
 - the poll tells '0 rivals found' apart from 'still discovering' (A1.1).
These use the in-memory repository (no DATABASE_URL in the test env)."""
from app.core import lifecycle
from app.db import repository
from app.models import CompetitiveReport


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
