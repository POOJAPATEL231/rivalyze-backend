"""The frozen /api/v1 contract. Owner: Drashti.

Two-phase pipeline (Rivalyze_TwoPhase_Pipeline.md):
  POST /api/v1/analyze            (auth) -> {job_id, status:"running_discovery"} (or completed on a persistence hit)
  POST /api/v1/analyze/company    (auth) -> same as /analyze, company + domain mode only
  POST /api/v1/analyze/idea       (auth) -> same as /analyze, idea mode only
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
from ..core.auth import get_current_user, require_token
from ..db import repository
from ..models import (
    AnalyzeCompanyRequest,
    AnalyzeIdeaRequest,
    AnalyzeRequest,
    AnalyzeResponse,
    CompetitiveReport,
    ConfirmRequest,
    EvidenceResponse,
    EvidenceRow,
    RunStatus,
    UserPublic,
)

router = APIRouter(prefix="/api/v1")


def _run_analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks,
                 user_id: str, cached: bool = False) -> AnalyzeResponse:
    """Shared logic behind /analyze, /analyze/company and /analyze/idea.

    DEFAULT: always run a fresh two-phase analysis. The stored report is tied to the
    rivals chosen LAST time, so silently returning it on a re-run showed stale data and
    blocked re-selecting rivals. The instant stored report is OPT-IN (cached=true); past
    reports are also available via GET /history + GET /reports/{run_id}.

    user_id is the authenticated caller; it flows into create_run so the run is owned
    and shows up in that user's (and only that user's) GET /history.
    """
    if req.company.strip() and cached:
        existing = lifecycle.find_completed(req.company)
        if existing:
            return AnalyzeResponse(job_id=existing, status="completed")
    return lifecycle.start_run(req, background_tasks, user_id)


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks,
            current_user: UserPublic = Depends(get_current_user),
            cached: bool = Query(False, description="opt in to instantly return the last "
                                 "completed report for this company instead of running a "
                                 "fresh analysis. Default runs fresh.")) -> AnalyzeResponse:
    if not req.company.strip() and not (req.idea or "").strip():
        raise HTTPException(status_code=422, detail="provide a company or an idea")
    return _run_analyze(req, background_tasks, current_user.user_id, cached)


@router.post("/analyze/company", response_model=AnalyzeResponse)
def analyze_company(req: AnalyzeCompanyRequest, background_tasks: BackgroundTasks,
                    current_user: UserPublic = Depends(get_current_user),
                    cached: bool = Query(False, description="opt in to return the last completed "
                                         "report instead of running a fresh analysis")) -> AnalyzeResponse:
    """Company + domain mode only — the split-out sibling of /analyze."""
    full_req = AnalyzeRequest(company=req.company, domain=req.domain, idea=None)
    return _run_analyze(full_req, background_tasks, current_user.user_id, cached)


@router.post("/analyze/idea", response_model=AnalyzeResponse)
def analyze_idea(req: AnalyzeIdeaRequest, background_tasks: BackgroundTasks,
                 current_user: UserPublic = Depends(get_current_user)) -> AnalyzeResponse:
    """Idea mode only — the split-out sibling of /analyze. No cached flag: idea mode
    has no company to match a prior report against. Optional structured intake
    (industry/geography/customer/model/stage) rides along so the idea pre-step can
    pin the market instead of guessing it from the sentence alone."""
    full_req = AnalyzeRequest(company="", domain="", idea=req.idea, idea_context=req.to_context())
    return _run_analyze(full_req, background_tasks, current_user.user_id)


@router.get("/runs/{job_id}", response_model=RunStatus, dependencies=[Depends(require_token)])
def get_run(job_id: str) -> RunStatus:
    job = lifecycle.get_run(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.post("/runs/{job_id}/confirm", response_model=AnalyzeResponse, status_code=202,
             dependencies=[Depends(require_token)],
             responses={
                 404: {"description": "job not found"},
                 409: {"description": "run is not awaiting confirmation "
                                       "(already confirmed, still discovering, or completed)"},
             })
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


@router.get("/evidence-refs", response_model=list[EvidenceRow], dependencies=[Depends(require_token)])
def get_evidence_by_ids_endpoint(run_id: str = Query(...),
                                 ids: str = Query(..., description="comma-separated ev- ids")) -> list[EvidenceRow]:
    """Fetch evidence rows by id — the drawer for a recommendation/opportunity,
    which cite evidence_ids (not a claim_ref). Declared BEFORE /evidence/{claim_ref}
    so the literal path wins over the path-param route. Order preserved to match the
    citation order; unknown ids are silently skipped (get_evidence_by_ids filters)."""
    if not repository.run_id_exists(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    id_list = [i.strip() for i in ids.split(",") if i.strip()]
    rows = repository.get_evidence_by_ids(id_list)
    # Scope to THIS run: get_evidence_by_ids resolves ids globally, so filter to the
    # requested run so a caller can't read another run's evidence by id.
    rows = [r for r in rows if str(r.get("run_id")) == str(run_id)]
    return [EvidenceRow.model_validate(r) for r in rows]


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


@router.get("/health/evidence")
def health_evidence() -> dict:
    """Live evidence-PERSISTENCE diagnostic. The report shows citations from an
    in-request index, but the citation drawer (GET /evidence/{claim_ref}) reads from
    Postgres — and merge writes evidence BEST-EFFORT (it swallows failures). So a
    missing `evidence` table, or a run_id that isn't a valid uuid in `runs` (FK
    violation), makes evidence silently NOT persist while the report still looks
    cited. This reports the truth from inside the deployment. Never raises."""
    import os

    out: dict = {"postgres": {"configured": bool(os.getenv("DATABASE_URL") or os.getenv("PGHOST"))}}
    if not out["postgres"]["configured"]:
        # No Postgres -> evidence lives only in the in-memory store: the drawer
        # works within a single process but nothing survives a restart.
        out["verdict"] = "in_memory_only"
        out["note"] = "No DATABASE_URL/PGHOST — evidence is in-memory only (fine locally, lost on restart)."
        return out

    try:
        with repository.get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.evidence')")
                table = cur.fetchone()[0] is not None
                out["postgres"]["evidence_table"] = table
                if table:
                    cur.execute("SELECT count(*) FROM evidence")
                    out["postgres"]["evidence_rows"] = cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        out["postgres"]["error"] = type(exc).__name__
        out["verdict"] = "db_error"
        return out

    table = out["postgres"].get("evidence_table")
    rows = out["postgres"].get("evidence_rows", 0)
    if not table:
        out["verdict"] = "table_missing"   # drawer will ALWAYS be empty — run app/db/schema.sql
    elif rows == 0:
        out["verdict"] = "table_empty"     # no evidence persisted yet: either no completed runs, or writes silently failing
    else:
        out["verdict"] = "persisting"      # evidence IS being written -> the citation drawer works
    out["drawer_working"] = out["verdict"] == "persisting"
    return out
