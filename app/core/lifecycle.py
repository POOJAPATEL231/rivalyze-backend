"""Run lifecycle — create the run, drive the pipeline in the background, and
own the event ledger the poller reads.

Owner: Drashti. The process must own NOTHING durable (restart-safe = TC-P01):
this in-memory JOBS dict is the POC stand-in that Saturday's build replaces
with Dharvi's Postgres repository (create_run / update_run_status /
append_events / save_report). Same shapes, so routes.py won't change when the
store does — only the four calls below get swapped for repository functions.

Exceptions inside the task are caught and turned into status=failed + an event;
the poller NEVER sees a 500 and never a stack trace.
"""
import time
import uuid

from fastapi import BackgroundTasks

from ..agents import discovery
from ..core import llm_router  # noqa: F401  (imported so MOCK env is resolved eagerly)
from ..core import search_chain as search_mod
from ..models import AnalyzeRequest, AnalyzeResponse, RunEvent, RunStatus

# POC-only store. Replace with Dharvi's repository on Saturday.
JOBS: dict[str, RunStatus] = {}


def _slug(text: str) -> str:
    return text.lower().strip().replace(" ", "-")[:24] or "idea"


def start_run(req: AnalyzeRequest, background_tasks: BackgroundTasks) -> AnalyzeResponse:
    """Create the run row, launch the pipeline, return IMMEDIATELY.

    The pipeline never runs synchronously — FastAPI BackgroundTasks executes
    _pipeline after the response is sent (sync fn -> Starlette threadpool, so
    the event loop is never blocked by httpx/sleep).
    """
    job_id = f"rivalyze-{_slug(req.company or (req.idea or ''))}-{uuid.uuid4().hex[:6]}"
    JOBS[job_id] = RunStatus(job_id=job_id, status="queued")
    background_tasks.add_task(_pipeline, job_id, req)
    return AnalyzeResponse(job_id=job_id, status="queued")


def get_run(job_id: str) -> RunStatus | None:
    return JOBS.get(job_id)


def _pipeline(job_id: str, req: AnalyzeRequest) -> None:
    job = JOBS[job_id]
    t0 = time.time()
    lane_stats: dict[str, int] = {}

    def emit(agent: str, msg: str) -> None:
        job.events.append(RunEvent(t=round(time.time() - t0, 1), agent=agent, msg=msg))
        if agent == "router":
            lane = msg.split("/")[0].split()[0]
            lane_stats[lane] = lane_stats.get(lane, 0) + (1 if "attempt" in msg or "MOCK" in msg else 0)

    try:
        job.status, job.current_stage = "running", "discovery"
        emit("system", f"run {job_id} started")
        result, lane = discovery.run(req.company, req.domain, emit)
        job.result = result
        job.lane_stats = {**lane_stats,
                          "searches": search_mod.stats["searches"],
                          "cache_hits": search_mod.stats["cache_hits"]}
        job.run_id = uuid.uuid4().hex  # stands in for the persisted report row id
        job.status, job.current_stage = "completed", "done"
        emit("system", f"completed in {time.time() - t0:.1f}s · lane={lane}")
    except Exception as e:  # belt-and-braces: still never a raw 500 to the poller
        job.status, job.error = "failed", str(e)
        emit("system", f"failed: {e}")
