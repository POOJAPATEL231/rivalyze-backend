"""The frozen /api/v1 contract. Owner: Drashti.

Endpoints in this slice:
  POST /api/v1/analyze      (auth) -> {job_id, status:"queued"} immediately
  GET  /api/v1/runs/{id}    (auth) -> poll shape (status, events, result, ...)
  GET  /api/v1/health       (open) -> {status:"ok", service:"rivalyze", counters:{...}}

Later routers register alongside this one in main.py (Gati /evidence,
Dharvi /history + /export + /reports, Sheel stretch /documents /chat). Drashti
owns the main.py includes, the auth dependency, and CORS.
"""
import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from ..core import lifecycle
from ..core.auth import require_token
from ..core.counters import counter_get, today_key
from ..models import AnalyzeRequest, AnalyzeResponse, RunStatus

router = APIRouter(prefix="/api/v1")

# health's `counters` field (added for Dharvi's warmup --budget flag, Module
# 4) reports today's call count for every provider budgets.json tracks.
# Loaded once at import (same "read once" convention as core/config.py); a
# missing/malformed file degrades to an empty provider list rather than
# crashing /health, which must never break.
_BUDGETS_PATH = Path(__file__).resolve().parents[2] / "budgets.json"
try:
    _BUDGET_PROVIDERS = list(json.loads(_BUDGETS_PATH.read_text(encoding="utf-8")))
except Exception:
    _BUDGET_PROVIDERS = []


@router.post("/analyze", response_model=AnalyzeResponse, dependencies=[Depends(require_token)])
def analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks) -> AnalyzeResponse:
    if not req.company.strip() and not (req.idea or "").strip():
        raise HTTPException(status_code=422, detail="provide a company or an idea")
    # persistence-first: if this company already has a completed run, hand it
    # back instantly — zero pipeline, zero credits.
    if req.company.strip():
        existing = lifecycle.find_completed(req.company)
        if existing:
            return AnalyzeResponse(job_id=existing, status="completed")
    return lifecycle.start_run(req, background_tasks)


@router.get("/runs/{job_id}", response_model=RunStatus, dependencies=[Depends(require_token)])
def get_run(job_id: str) -> RunStatus:
    job = lifecycle.get_run(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/health")
def health() -> dict:
    counters = {p: counter_get(today_key(p)) for p in _BUDGET_PROVIDERS}
    return {"status": "ok", "service": "rivalyze", "counters": counters}
