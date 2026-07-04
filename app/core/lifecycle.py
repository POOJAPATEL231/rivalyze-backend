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

from fastapi import BackgroundTasks

from ..agents import discovery
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
    CompetitorSet,
    RunEvent,
    RunStatus,
)

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


def _emitter(job_id: str):
    """A DB-backed event emitter closed over the run's start time, plus a live
    lane_stats accumulator. Returns (emit, lane_stats, t0)."""
    t0 = time.time()
    lane_stats: dict[str, int] = {}

    def emit(agent: str, msg: str) -> None:
        repository.append_events(
            job_id, [{"t": round(time.time() - t0, 1), "agent": agent, "msg": msg}]
        )
        if agent == "router":
            lane = msg.split("/")[0].split()[0]
            lane_stats[lane] = lane_stats.get(lane, 0) + (1 if "attempt" in msg or "MOCK" in msg else 0)

    return emit, lane_stats, t0


def _persist_lane_stats(job_id: str, lane_stats: dict) -> None:
    repository.set_lane_stats(job_id, {
        **lane_stats,
        "searches": search_mod.stats["searches"],
        "cache_hits": search_mod.stats["cache_hits"],
    })


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
        orchestrator.run_discovery(state, _agents(), emit)
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
        if report_dict:
            repository.save_report(run_id, report_dict)
            repository.finish_run(job_id, report_dict.get("threat_level"),
                                  _report_confidence(report_dict))
            emit("system", f"report ready · threat={report_dict.get('threat_level')}")
        else:
            # Degraded run: no valid report survived validation. Still a completed
            # run (never a 500), just without a persisted CompetitiveReport.
            repository.finish_run(job_id)
            emit("system", "analysis completed with degraded (empty) report")

        _persist_lane_stats(job_id, lane_stats)
        emit("system", f"completed in {time.time() - t0:.1f}s")
    except Exception:
        logger.exception("analysis %s failed", job_id)
        repository.fail_run(job_id, "internal pipeline error")
        emit("system", "failed: internal error")


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
    result = CompetitorSet(competitors=[Competitor(**c) for c in competitors]) if competitors else None
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
