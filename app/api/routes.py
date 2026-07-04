"""The frozen /api/v1 contract. Owner: Drashti.

Endpoints in this slice:
  POST /api/v1/analyze      (auth) -> {job_id, status:"queued"} immediately
  GET  /api/v1/runs/{id}    (auth) -> poll shape (status, events, result, ...)
  GET  /api/v1/health       (open) -> {status:"ok", service:"rivalyze"}

Later routers register alongside this one in main.py (Gati /evidence,
Dharvi /history + /export + /reports, Sheel stretch /documents /chat). Drashti
owns the main.py includes, the auth dependency, and CORS.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from ..core import lifecycle
from ..core.auth import require_token
from ..models import AnalyzeRequest, AnalyzeResponse, RunStatus

router = APIRouter(prefix="/api/v1")


@router.post("/analyze", response_model=AnalyzeResponse, dependencies=[Depends(require_token)])
def analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks) -> AnalyzeResponse:
    if not req.company.strip() and not (req.idea or "").strip():
        raise HTTPException(status_code=422, detail="provide a company or an idea")
    # TODO (persistence-first re-run): when the repository lands, short-circuit
    # here if find_completed_report(slug) hits -> return existing job_id/completed.
    return lifecycle.start_run(req, background_tasks)


@router.get("/runs/{job_id}", response_model=RunStatus, dependencies=[Depends(require_token)])
def get_run(job_id: str) -> RunStatus:
    job = lifecycle.get_run(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "rivalyze"}
