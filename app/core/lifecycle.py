"""Run lifecycle — create the run, drive the pipeline in the background, and
own the event ledger the poller reads.

Owner: Drashti. The process owns NOTHING durable (restart-safe = TC-P01):
every run, event, and competitor lives in Postgres via the repository. A
restart loses nothing; the frontend polls the run straight from the database.

Exceptions inside the task are caught and turned into status=failed + an event;
the poller NEVER sees a 500 and never a stack trace.
"""
import logging
import re
import time
import uuid

from fastapi import BackgroundTasks

from ..agents import discovery
from ..core import llm_router  # noqa: F401  (imported so MOCK env is resolved eagerly)
from ..core import search_chain as search_mod
from ..db import repository
from ..models import AnalyzeRequest, AnalyzeResponse, RunStatus

logger = logging.getLogger(__name__)


def _slug(text: str) -> str:
    # Keep only url/path/log-safe chars — never let raw user text (spaces,
    # slashes, control chars) leak into the job_id.
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:24] or "idea"


def find_completed(company: str) -> str | None:
    """Persistence-first lookup — an existing completed run's job_id, or None."""
    return repository.find_completed_run(company)


def start_run(req: AnalyzeRequest, background_tasks: BackgroundTasks) -> AnalyzeResponse:
    """Create the run row in Postgres, launch the pipeline, return IMMEDIATELY.

    The pipeline never runs synchronously — FastAPI BackgroundTasks executes
    _pipeline after the response is sent (sync fn -> Starlette threadpool, so
    the event loop is never blocked by httpx/sleep).
    """
    name = req.company or (req.idea or "")
    job_id = f"rivalyze-{_slug(name)}-{uuid.uuid4().hex[:6]}"
    run_id = repository.create_run(job_id, name, req.domain)
    background_tasks.add_task(_pipeline, job_id, run_id, req)
    return AnalyzeResponse(job_id=job_id, status="queued")


def get_run(job_id: str) -> RunStatus | None:
    return repository.get_run(job_id)


def _pipeline(job_id: str, run_id: str, req: AnalyzeRequest) -> None:
    t0 = time.time()
    lane_stats: dict[str, int] = {}

    def emit(agent: str, msg: str) -> None:
        repository.append_events(
            job_id, [{"t": round(time.time() - t0, 1), "agent": agent, "msg": msg}]
        )
        if agent == "router":
            lane = msg.split("/")[0].split()[0]
            lane_stats[lane] = lane_stats.get(lane, 0) + (1 if "attempt" in msg or "MOCK" in msg else 0)

    try:
        repository.update_run_status(job_id, "running", "discovery")
        emit("system", f"run {job_id} started")
        # discovery persists its competitors via the repository using run_id
        discovery.run(req.company, req.domain, run_id, emit)
        repository.set_lane_stats(job_id, {**lane_stats,
                                           "searches": search_mod.stats["searches"],
                                           "cache_hits": search_mod.stats["cache_hits"]})
        repository.finish_run(job_id, "completed")
        emit("system", f"completed in {time.time() - t0:.1f}s")
    except Exception:  # belt-and-braces: still never a raw 500 to the poller
        # Log the full detail server-side; persist only a generic, user-safe line
        # (schema.sql documents runs.error as "one line, user-safe" — enforce it).
        logger.exception("pipeline %s failed", job_id)
        repository.finish_run(job_id, "failed", "internal pipeline error")
        emit("system", "failed: internal error")
