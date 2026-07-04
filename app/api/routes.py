"""The frozen /api/v1 contract. Owner: Drashti.

Two-phase pipeline (Rivalyze_TwoPhase_Pipeline.md):
  POST /api/v1/analyze            (auth) -> {job_id, status:"running_discovery"} (or completed on a persistence hit)
  GET  /api/v1/runs/{id}          (auth) -> poll shape; parks at awaiting_confirmation with competitors
  POST /api/v1/runs/{id}/confirm  (auth) -> {job_id, status:"confirmed"} — the "Deploy the agents" button
  GET  /api/v1/reports/{run_id}   (auth) -> the full CompetitiveReport
  GET  /api/v1/evidence/{ref}     (auth) -> {claim_ref, sources[]} — the citation drawer
  GET  /api/v1/history            (auth) -> completed runs, newest first
  GET  /api/v1/reports/{id}/export(auth) -> text/markdown attachment
  GET  /api/v1/health             (open) -> {status:"ok", service:"rivalyze"}
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from ..core import export, lifecycle
from ..core.auth import require_token
from ..db import repository
from ..models import (
    AnalyzeRequest,
    AnalyzeResponse,
    CompetitiveReport,
    ConfirmRequest,
    EvidenceResponse,
    EvidenceRow,
    HistoryRow,
    RunStatus,
)

router = APIRouter(prefix="/api/v1")


@router.post("/analyze", response_model=AnalyzeResponse, dependencies=[Depends(require_token)])
def analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks) -> AnalyzeResponse:
    if not req.company.strip() and not (req.idea or "").strip():
        raise HTTPException(status_code=422, detail="provide a company or an idea")
    # persistence-first: if this company already has a completed report, hand it
    # back instantly — zero pipeline, zero credits (skips BOTH phases).
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


@router.post("/runs/{job_id}/confirm", response_model=AnalyzeResponse, status_code=202,
             dependencies=[Depends(require_token)])
def confirm(job_id: str, req: ConfirmRequest, background_tasks: BackgroundTasks) -> AnalyzeResponse:
    """Phase 2 launch. Validates the run exists AND is awaiting_confirmation, then
    persists the user-edited competitor list and kicks off the analysis graph."""
    if lifecycle.get_run(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    # Atomic compare-and-swap: only an awaiting run flips to confirmed, and only
    # ONE concurrent caller wins — so a second /confirm (or a confirm on a
    # running/completed run) gets 409 and the agents launch exactly once.
    run_id = repository.confirm_run(job_id)
    if run_id is None:
        raise HTTPException(status_code=409, detail="run is not awaiting confirmation")
    rows = [c.model_dump() for c in req.confirmed_competitors]
    repository.replace_competitors(run_id, rows)
    background_tasks.add_task(lifecycle.start_analysis, job_id, run_id, rows)
    return AnalyzeResponse(job_id=job_id, status="confirmed")


@router.get("/reports/{run_id}", response_model=CompetitiveReport, dependencies=[Depends(require_token)])
def get_report_endpoint(run_id: str) -> CompetitiveReport:
    row = repository.get_report(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    return CompetitiveReport.model_validate(row["report"])


@router.get("/evidence/{claim_ref}", response_model=EvidenceResponse, dependencies=[Depends(require_token)])
def get_evidence_endpoint(claim_ref: str, run_id: str = Query(...)) -> EvidenceResponse:
    # 404 ONLY when the run itself is unknown; an unknown claim_ref is a valid
    # 200 with an empty sources list (this endpoint IS the "check it, click it" line).
    if not repository.run_id_exists(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    rows = repository.get_evidence(run_id, claim_ref)
    return EvidenceResponse(claim_ref=claim_ref,
                            sources=[EvidenceRow.model_validate(r) for r in rows])


@router.get("/history", response_model=list[HistoryRow], dependencies=[Depends(require_token)])
def get_history_endpoint(company: str | None = Query(default=None)) -> list[HistoryRow]:
    rows = repository.get_history(company=company)
    return [
        HistoryRow(
            job_id=r["job_id"],
            company=r["company"],
            threat_level=r["threat_level"],
            confidence=float(r["confidence"]) if r["confidence"] is not None else None,
            created_at=r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
        )
        for r in rows
    ]


@router.get("/reports/{run_id}/export", dependencies=[Depends(require_token)])
def export_report(run_id: str, format: str = Query(default="md")) -> PlainTextResponse:
    if format != "md":
        raise HTTPException(status_code=422, detail="unsupported format (only 'md')")
    row = repository.get_report(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    report = CompetitiveReport.model_validate(row["report"])
    if row.get("md_export"):
        md = row["md_export"]
    else:
        md = export.report_to_markdown(report)
    return PlainTextResponse(
        md, media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="rivalyze-{run_id}.md"'},
    )


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "rivalyze"}
