"""Regression tests for the lifecycle audit fixes:
 - a completed, report-bearing run is never flipped to 'failed' by post-completion
   bookkeeping (A1.2);
 - a degraded run still persists an honest report shell so /reports never 404s (A1.4);
 - the poll tells '0 rivals found' apart from 'still discovering' (A1.1).
These use the in-memory repository (no DATABASE_URL in the test env)."""
import pytest

from app.core import lifecycle
from app.db import connection, repository
from app.db.repository import _MemStore
from app.models import AnalyzeRequest, CompetitiveReport


@pytest.fixture(autouse=True)
def _force_memstore(monkeypatch):
    """Pin these tests to the in-memory repository EVEN when PG*/DATABASE_URL is
    configured. They use hardcoded job_ids ("poll-job-1", ...), so running them
    against a real shared database inserts rows that collide on the second run
    (UniqueViolation on runs_job_id_key) and pollute shared state. A fresh
    _MemStore per test also isolates them from each other."""
    monkeypatch.setattr(connection, "is_enabled", lambda: False)
    monkeypatch.setattr(repository, "_mem", _MemStore())


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


def test_emitter_tracks_live_header_metrics():
    import app.core.search_chain as sc
    cid = repository.create_company("MetricCo", "")
    repository.create_run("metric-job", cid)
    emit, stats, _ = lifecycle._emitter("metric-job")

    emit("router", "gemini/gemini-2.5-flash · attempt 1")
    emit("router", "cerebras/gpt-oss-120b · attempt 1")
    emit("merge", "fused 17 signals · 28 evidence rows · 4 rivals")
    sc.stats["searches"] += 3                       # simulate 3 real searches this run
    emit("search", '"q" · tavily')

    assert stats["llm_calls"] == 2                  # summed across lanes
    assert stats["signals_found"] == 17
    assert stats["evidence_rows"] == 28
    assert stats["searches"] >= 3
    # writes are THROTTLED (~1.5s) now that emit() is called concurrently, so a burst
    # persists at least once mid-run, and the end-of-run force flush lands the finals.
    assert repository.get_run("metric-job")["lane_stats"]        # something persisted live
    lifecycle._persist_lane_stats("metric-job", stats)          # final flush (as the pipeline does)
    persisted = repository.get_run("metric-job")["lane_stats"]
    assert persisted["llm_calls"] == 2 and persisted["signals_found"] == 17


def test_metrics_accumulate_across_phases():
    # phase 2's emitter must build on phase 1's counts, not overwrite them
    cid = repository.create_company("AccumCo", "")
    repository.create_run("accum-job", cid)
    repository.set_lane_stats("accum-job", {"llm_calls": 4, "searches": 2})
    _, stats, _ = lifecycle._emitter("accum-job")
    assert stats["llm_calls"] == 4 and stats["searches"] == 2   # seeded from prior phase


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
