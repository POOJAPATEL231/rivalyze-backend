"""Run lifecycle — create the run, drive the TWO-PHASE pipeline in the background,
and own the event ledger the poller reads.

Owner: Drashti. Two-phase split at the human-approval gate
(Rivalyze_TwoPhase_Pipeline.md):
  Phase 1 (/analyze)  -> start_discovery: discovery only, park at awaiting_confirmation.
  Phase 2 (/confirm)  -> start_analysis:  news‖product‖review -> merge -> strategist
                         -> validate, on the user-confirmed competitor list.

Both phases run the LangGraph orchestrator's two graphs over one PipelineState and
are wrapped so an exception becomes status=failed + one user-safe event — the
poller NEVER sees a 500 or a stack trace. The process owns NOTHING durable
(restart-safe = TC-P01): every run, event, competitor, and report lives in Postgres.
"""
import logging
import re
import time
import uuid
from datetime import datetime

from fastapi import BackgroundTasks

from ..agents import discovery
from ..core import config
from ..core import confidence as confidence_mod
from ..core import llm_router  # noqa: F401  (imported so MOCK env is resolved eagerly)
from ..core import merge as merge_mod
from ..core import orchestrator
from ..core import search_chain as search_mod
from ..db import repository
from ..models import (
    AnalyzeRequest,
    AnalyzeResponse,
    Competitor,
    CompetitiveReport,
    CompetitorSet,
    RunEvent,
    RunStatus,
    Swot,
)

# Statuses at/after which discovery has finished — used to tell "discovery done,
# 0 rivals found" apart from "still discovering" in the poll shape.
_DISCOVERY_DONE = {"awaiting_confirmation", "confirmed", "running_analysis", "completed"}

logger = logging.getLogger(__name__)

# Lazily-built agent mapping the orchestrator's graphs consume. Built once, on
# first use, so importing lifecycle doesn't eagerly import every agent (and any
# import error surfaces inside the guarded background task, not at app startup).
_AGENTS = None


def _agents() -> dict:
    global _AGENTS
    if _AGENTS is None:
        from ..agents import news, product, review, strategist
        _AGENTS = {
            "discovery": orchestrator.discovery_node(discovery.run),
            "news": orchestrator.gather_node("news", news.run),
            "product": orchestrator.gather_node("product", product.run),
            "review": orchestrator.gather_node("review", review.run),
            "merge": merge_mod.merge_node,
            "strategist": orchestrator.strategist_node(strategist.run, confidence_mod.confidence),
        }
    return _AGENTS


def _slug(text: str) -> str:
    # Keep only url/path/log-safe chars — never let raw user text (spaces,
    # slashes, control chars) leak into the job_id.
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:24] or "idea"


# Header metric keys the UI shows (LLM CALLS / SEARCHES / SIGNALS FOUND). Tracked
# per-run and persisted LIVE so the counters tick up during the run, not just at end.
_METRIC_KEYS = ("llm_calls", "searches", "cache_hits", "signals_found", "evidence_rows")


def _emitter(job_id: str):
    """A DB-backed event emitter plus a LIVE per-run metrics accumulator.

    Returns (emit, stats, t0). `stats` carries both per-lane attempt counts and the
    header metrics (llm_calls / searches / cache_hits / signals_found / evidence_rows)
    and is written to the run row on every change, so the UI's live counters update
    mid-run instead of staying 0 until the end. Phase 2 SEEDS from Phase 1's stats so
    counts accumulate across phases rather than the second phase overwriting the first.
    """
    t0 = time.time()
    # search_chain.stats is a process-global cumulative counter; snapshot it so the
    # DELTA is attributed to THIS run (exact for the typical one-run-at-a-time case).
    search_base = dict(search_mod.stats)
    existing = (repository.get_run(job_id) or {}).get("lane_stats") or {}
    stats: dict[str, int] = {k: int(v) for k, v in existing.items() if isinstance(v, (int, float))}
    for k in _METRIC_KEYS:
        stats.setdefault(k, 0)
    prior_searches, prior_cache = stats["searches"], stats["cache_hits"]

    def _flush() -> None:
        try:
            repository.set_lane_stats(job_id, dict(stats))
        except Exception:  # noqa: BLE001 — a live-metric write must never break the run
            pass

    def emit(agent: str, msg: str) -> None:
        repository.append_events(
            job_id, [{"t": round(time.time() - t0, 1), "agent": agent, "msg": msg}]
        )
        changed = False
        if agent == "router" and ("attempt" in msg or "MOCK" in msg):
            stats["llm_calls"] += 1
            lane = msg.split("/")[0].split()[0]
            stats[lane] = int(stats.get(lane, 0)) + 1
            changed = True
        elif agent == "search":
            stats["searches"] = prior_searches + (search_mod.stats["searches"] - search_base["searches"])
            stats["cache_hits"] = prior_cache + (search_mod.stats["cache_hits"] - search_base["cache_hits"])
            changed = True
        elif agent == "merge":
            m = re.search(r"fused\s+(\d+)\s+signals.*?(\d+)\s+evidence", msg)
            if m:
                stats["signals_found"], stats["evidence_rows"] = int(m.group(1)), int(m.group(2))
                changed = True
        if changed:
            _flush()

    return emit, stats, t0


def _persist_lane_stats(job_id: str, lane_stats: dict) -> None:
    """Final durable flush of the run's stats (they are already written live by the
    emitter; this just guarantees the last values land)."""
    try:
        repository.set_lane_stats(job_id, dict(lane_stats))
    except Exception:  # noqa: BLE001
        pass


def _report_confidence(report: dict) -> float | None:
    """Run-level confidence for the history/dashboard header: the mean of the
    (already code-computed) recommendation confidences, or None if there are none."""
    recs = report.get("recommendations") or []
    vals = [r.get("confidence") for r in recs if isinstance(r.get("confidence"), (int, float))]
    if not vals:
        return None
    return round(max(0.05, min(0.95, sum(vals) / len(vals))), 2)


# ================================ persistence-first ================================
def find_completed(company: str) -> str | None:
    """Persistence-first lookup — an existing completed run's job_id, or None."""
    found = repository.find_completed_report(company)
    return found["job_id"] if found else None


# ==================================== phase 1 ====================================
def start_run(req: AnalyzeRequest, background_tasks: BackgroundTasks) -> AnalyzeResponse:
    """Create the run row, launch Phase 1 (discovery), return IMMEDIATELY.

    The pipeline never runs synchronously — FastAPI BackgroundTasks executes
    start_discovery after the response is sent (sync fn -> Starlette threadpool,
    so the event loop is never blocked by httpx/sleep).
    """
    name = req.company or (req.idea or "")
    job_id = f"rivalyze-{_slug(name)}-{uuid.uuid4().hex[:6]}"
    company_id = repository.create_company(name, req.domain)
    run_id = repository.create_run(job_id, company_id)
    background_tasks.add_task(start_discovery, job_id, run_id, req)
    return AnalyzeResponse(job_id=job_id, status="running_discovery")


def start_discovery(job_id: str, run_id: str, req: AnalyzeRequest) -> None:
    """Phase 1: run graph_discovery, then PARK at awaiting_confirmation.

    Discovery persists its competitors via the repository using run_id; the poller
    reads them straight from Postgres, so the gate needs only the status flip.
    """
    emit, lane_stats, t0 = _emitter(job_id)
    try:
        repository.update_run_status(job_id, "running_discovery", "discovery")
        emit("system", f"run {job_id} · discovery started")
        state = {"company": req.company, "domain": req.domain,
                 "idea": req.idea, "run_id": run_id}
        final_state = orchestrator.run_discovery(state, _agents(), emit)
        # Idea mode: discovery resolved a real company/domain from the idea, but the
        # company row still holds the raw idea sentence. Persist the resolved identity
        # so Phase 2 (which re-reads the company from Postgres) stamps the report with
        # the company name, not the whole idea paragraph.
        if (req.idea or "").strip() and not (req.company or "").strip():
            resolved = (final_state or {}).get("company")
            if resolved and resolved.strip() and resolved.strip() != (req.idea or "").strip():
                repository.set_run_company(run_id, resolved.strip(),
                                           (final_state or {}).get("domain") or "")
        _persist_lane_stats(job_id, lane_stats)
        repository.update_run_status(job_id, "awaiting_confirmation", "awaiting_confirmation")
        emit("system", f"discovery complete in {time.time() - t0:.1f}s · awaiting confirmation")
    except Exception:  # belt-and-braces: never a raw 500 to the poller
        logger.exception("discovery %s failed", job_id)
        repository.fail_run(job_id, "internal pipeline error")
        emit("system", "failed: internal error")


# ==================================== phase 2 ====================================
def start_analysis(job_id: str, run_id: str, confirmed: list[dict]) -> None:
    """Phase 2: run graph_analysis on the user-confirmed competitors, persist the
    report + threat/confidence, mark completed. Launched by POST /confirm."""
    emit, lane_stats, t0 = _emitter(job_id)
    try:
        repository.update_run_status(job_id, "running_analysis", "news")
        emit("system", "analysis started")
        company = (repository.get_run_company(run_id) or {}).get("name", "") or ""
        competitors = [Competitor(**c) for c in confirmed]
        state = {"run_id": run_id, "company": company, "competitors": competitors}
        final = orchestrator.run_analysis(state, _agents(), emit)

        report = final.get("report")
        report_dict = report.model_dump() if hasattr(report, "model_dump") else report
        if not report_dict:
            # Degraded run: no valid report survived validation. Persist an HONEST
            # shell (not nothing) so GET /reports/{run_id} returns 200 with an
            # "insufficient signal" report instead of a 404 dead-end — the poll
            # exposes run_id for any completed run.
            report_dict = _degraded_report_shell(company, final)
            emit("system", "analysis completed with degraded (low-signal) report")
        repository.save_report(run_id, report_dict)
        repository.finish_run(job_id, report_dict.get("threat_level"),
                              _report_confidence(report_dict))
    except Exception:
        logger.exception("analysis %s failed", job_id)
        repository.fail_run(job_id, "internal pipeline error")
        emit("system", "failed: internal error")
        return

    # Post-completion bookkeeping. The run is ALREADY completed+persisted; a failure
    # here (a transient DB blip on the event/lane-stats write) must NOT flip a good,
    # report-bearing run to "failed" — which would strand the UI away from a report
    # that is sitting in the DB. So it runs outside the try above and is swallowed.
    try:
        # Optional report-quality scoring (adopted from rivalyze-dev). Best-effort,
        # OFF by default (one extra LLM call); the score rides in lane_stats.
        if config.REPORT_EVAL:
            from ..core import report_eval
            scores = report_eval.evaluate(report_dict, company, emit)
            if scores:
                lane_stats["report_score"] = scores["overall_score"]
        _persist_lane_stats(job_id, lane_stats)
        emit("system", f"report ready · completed in {time.time() - t0:.1f}s")
    except Exception:  # noqa: BLE001 — cosmetic; the run is already completed
        logger.warning("analysis %s: post-completion bookkeeping failed (run already completed)", job_id)


def _degraded_report_shell(company: str, final: dict) -> dict:
    """A minimal, honest CompetitiveReport for a run whose synthesis degraded — so
    the report route returns 200 with a clear 'insufficient signal' message and the
    low_signal_findings, instead of the UI hitting a 404 on a 'completed' run."""
    findings = list(final.get("low_signal_findings") or [])
    return CompetitiveReport(
        company=company or "our company",
        threat_level="MEDIUM",
        executive_summary=("This analysis could not gather enough signal to produce a full "
                           "competitive report. Please try again, or re-run with different rivals."),
        swot=Swot(), sentiment={}, head_to_head=[], opportunities=[], recommendations=[],
        low_signal_findings=findings or ["analysis: degraded run — insufficient signal"],
        analysis_date=datetime.now().strftime("%Y-%m-%d"),
    ).model_dump()


# ===================================== poll =====================================
def get_run(job_id: str) -> RunStatus | None:
    """Read the run row + its competitors and assemble the poll shape.

    repository.get_run() stays a flat DB-shape dict; this is where that row becomes
    the typed RunStatus the API contract promises, joining in competitors via
    repository.get_competitors(). At awaiting_confirmation, result.competitors is
    the PROPOSED set the UI edits and posts back to /confirm.
    """
    row = repository.get_run(job_id)
    if row is None:
        return None
    competitors = repository.get_competitors(row["id"])
    if competitors:
        result = CompetitorSet(competitors=[Competitor(**c) for c in competitors])
    elif row["status"] in _DISCOVERY_DONE:
        # Discovery finished but found 0 rivals: return an EMPTY set (not None) so the
        # UI can tell "done, none found — add your own" apart from "still discovering",
        # and isn't stranded at awaiting_confirmation with an ambiguous null result.
        result = CompetitorSet(competitors=[])
    else:
        result = None
    return RunStatus(
        job_id=row["job_id"],
        status=row["status"],
        current_stage=row["current_stage"],
        events=[RunEvent(**e) for e in (row["events"] or [])],
        result=result,
        lane_stats=row["lane_stats"] or {},
        run_id=row["id"] if row["status"] == "completed" else None,
        error=row["error"],
    )
