"""The frozen /api/v1 contract. Owner: Drashti.

Two-phase pipeline (Rivalyze_TwoPhase_Pipeline.md):
  POST /api/v1/analyze            (auth) -> {job_id, status:"running_discovery"} (or completed on a persistence hit)
  GET  /api/v1/runs/{id}          (auth) -> poll shape; parks at awaiting_confirmation with competitors
  POST /api/v1/runs/{id}/confirm  (auth) -> {job_id, status:"confirmed"} — the "Deploy the agents" button
  GET  /api/v1/reports/{run_id}   (auth) -> the full CompetitiveReport
  GET  /api/v1/evidence/{ref}     (auth) -> {claim_ref, sources[]} — the citation drawer
  GET  /api/v1/health             (open) -> {status:"ok", service:"rivalyze"}

GET /history and GET /reports/{id}/export live in app/api/history_routes.py
(Dharvi's router), registered alongside this one in main.py.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from ..core import lifecycle
from ..core.auth import require_token
from ..db import repository
from ..models import (
    AnalyzeRequest,
    AnalyzeResponse,
    CompetitiveReport,
    ConfirmRequest,
    EvidenceResponse,
    EvidenceRow,
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


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "rivalyze"}


@router.get("/health/cache")
def health_cache() -> dict:
    """Live cache diagnostic — runs INSIDE the deployment, so it reports the real
    REDIS_URL / DATABASE_URL that only exist in App Service config (not in the repo
    or a local .env). Leaks NO secret values: only the url scheme, booleans, and a
    verdict. Never raises — every check is guarded so /health/cache itself is safe."""
    import os

    out: dict = {"redis": {}, "postgres": {}, "roundtrip": {}}

    # --- Redis hot layer ---
    redis_url = os.getenv("REDIS_URL", "")
    scheme = redis_url.split("://", 1)[0] if "://" in redis_url else ""
    valid_scheme = scheme in ("redis", "rediss", "unix")
    out["redis"] = {"configured": bool(redis_url), "scheme": scheme or None,
                    "valid_scheme": valid_scheme, "ping": None}
    if redis_url and valid_scheme:
        try:
            import redis as _redis
            out["redis"]["ping"] = bool(
                _redis.Redis.from_url(redis_url, socket_timeout=5,
                                      decode_responses=True).ping())
        except Exception as exc:  # noqa: BLE001
            out["redis"]["ping"] = False
            out["redis"]["error"] = type(exc).__name__
    elif redis_url and not valid_scheme:
        out["redis"]["error"] = "REDIS_URL has no scheme (needs rediss://…) — silently disabled"

    # --- Postgres write-through fallback ---
    pg_configured = bool(os.getenv("DATABASE_URL") or os.getenv("PGHOST"))
    out["postgres"] = {"configured": pg_configured, "search_cache_table": None}
    if pg_configured:
        try:
            with repository.get_pool().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.search_cache')")
                    out["postgres"]["search_cache_table"] = cur.fetchone()[0] is not None
        except Exception as exc:  # noqa: BLE001
            out["postgres"]["search_cache_table"] = False
            out["postgres"]["error"] = type(exc).__name__

    # --- real set→get through the app's own cache module ---
    try:
        from ..core import cache
        key = cache.make_cache_key("__health_cache_probe__")
        cache.cache_set(key, {"probe": "ok"})
        got = cache.cache_get(key)
        out["roundtrip"]["ok"] = bool(got and got.get("probe") == "ok")
    except Exception as exc:  # noqa: BLE001
        out["roundtrip"] = {"ok": False, "error": type(exc).__name__}

    redis_ok = out["redis"].get("ping") is True
    pg_ok = out["postgres"].get("search_cache_table") is True
    if redis_ok:
        verdict = "redis_active"
    elif pg_ok:
        verdict = "postgres_only_active"
    else:
        verdict = "no_working_cache"
    out["verdict"] = verdict
    out["cache_working"] = redis_ok or pg_ok
    return out
