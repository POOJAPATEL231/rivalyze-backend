"""Monitor Delta v0 — company-scoped routes. Owner: Drashti.

  GET  /api/v1/companies/{slug}/delta    "what's new since last run": diffs the
       signals of the two most-recent COMPLETED runs (dedupe logic in
       app/core/delta.py). Pure DB read — NO AI call, NO search, NO credits.

  POST /api/v1/companies/{slug}/refresh  the "check for updates" trigger: starts
       a NEW analysis run reusing the rivals the user confirmed LAST time, so it
       skips discovery + the confirmation gate and launches phase 2 directly.
       This is the manual version of the future weekly timer — after it
       completes, GET .../delta shows what changed. Spends credits (agents run).

Registered alongside routes.py in main.py; reuses the shared require_token.

.NET reader mapping: delta is a thin GET over a "diff two result sets" query —
like a stored proc comparing this week's rows to last week's; refresh is a
fire-and-forget job enqueue returning 202 + the job id to poll.
"""
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from ..core import lifecycle
from ..core.auth import require_token
from ..core.delta import compute_delta
from ..db import repository
from ..models import AnalyzeResponse, DeltaResponse

router = APIRouter(prefix="/api/v1")


@router.get("/companies/{slug}/delta", response_model=DeltaResponse,
            response_model_exclude_none=True, dependencies=[Depends(require_token)])
def company_delta(slug: str) -> DeltaResponse:
    """Two 200 shapes (exclude_none drops the unused optionals):
      - previous run exists: {company, since, count, new_signals}
      - zero or one completed run: {count: 0, new_signals: [], first_run: true}
    404 only for an unknown slug."""
    company = repository.get_company_by_slug(slug)
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")

    runs = repository.get_latest_completed_runs(company["id"], limit=2)
    if len(runs) < 2:
        return DeltaResponse(count=0, new_signals=[], first_run=True)

    r1, r0 = runs[0], runs[1]  # latest, previous
    new = compute_delta(repository.get_signals_for_run(r0["id"]),
                        repository.get_signals_for_run(r1["id"]))
    return DeltaResponse(company=company["name"], since=r0["finished_at"],
                        count=len(new), new_signals=new)


@router.post("/companies/{slug}/refresh", response_model=AnalyzeResponse, status_code=202,
             dependencies=[Depends(require_token)])
def refresh_company(slug: str, background_tasks: BackgroundTasks) -> AnalyzeResponse:
    """Re-run the analysis for a company that was analyzed before.

    Reuses the competitor list the user confirmed on the LATEST completed run —
    a monitoring refresh must diff like-for-like, and it means no confirmation
    gate: phase 2 launches immediately in the background. The client polls
    GET /runs/{job_id} and, on completed, fetches GET /companies/{slug}/delta.

    409 when the company has no completed run (or none with confirmed rivals):
    the FIRST analysis must go through the normal two-phase flow, because only
    a user can confirm which rivals matter."""
    company = repository.get_company_by_slug(slug)
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")

    runs = repository.get_latest_completed_runs(company["id"], limit=1)
    if not runs:
        raise HTTPException(status_code=409,
                            detail="no completed analysis to refresh — run a full analysis first")
    rivals = repository.get_competitors(runs[0]["id"])
    if not rivals:
        raise HTTPException(status_code=409,
                            detail="previous run has no confirmed competitors — run a full analysis first")

    job_id = f"rivalyze-{lifecycle._slug(company['name'])}-{uuid.uuid4().hex[:6]}"
    run_id = repository.create_run(job_id, company["id"])
    repository.replace_competitors(run_id, rivals)
    background_tasks.add_task(lifecycle.start_analysis, job_id, run_id, rivals)
    return AnalyzeResponse(job_id=job_id, status="running_analysis")
