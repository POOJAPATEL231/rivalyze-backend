# Rivalyze — backend
Multi-agent competitive intelligence. 5 AI agents in a 7-node LangGraph pipeline:
discovery → [news ‖ product ‖ review] → merge → strategist → validate.
Every claim is backed by an evidence row; confidence is computed, never model-asserted.

## Run
    pip install -r requirements.txt
    MOCK_MODE=1 uvicorn app.main:app --port 8000   # offline, zero keys
    # PowerShell: $env:MOCK_MODE="1"; uvicorn app.main:app --port 8000
    # live: cp .env.example .env, add any subset of keys — the router skips missing lanes

Open http://localhost:8000, hit **Analyze**, watch the event ledger, see the typed result.

## Contract (frozen — /api/v1)
    POST /api/v1/analyze   (Bearer) {company, domain, idea?} → {job_id, status:"queued"}
    GET  /api/v1/runs/{id} (Bearer) → {status, current_stage, events[], result, lane_stats, run_id, error}
    GET  /api/v1/health    (open)   → {status:"ok", service:"rivalyze"}
Auth is open when BEARER_TOKEN is unset (local/MOCK dev); set it to lock the surface.

## Architecture
One LLM code path (`app/core/llm_router.py`) — every provider is a config row.
One database (Azure PostgreSQL, `app/db/schema.sql`) — Redis is a disposable write-through cache.
Module map + owners: see docstrings per module; contract in `app/api/routes.py`.
Tests: `pytest` (contract + node-boundary suites).

## Layout
    app/
      main.py            app assembly: CORS, router includes, minimal UI
      models.py          THE shared Pydantic contract (all node boundaries validate against it)
      api/routes.py      the /api/v1 endpoints
      core/
        config.py        env → resolved settings
        auth.py          Bearer dependency
        lifecycle.py     run creation + background pipeline + event ledger
        llm_router.py    4-lane OpenAI-dialect failover (MOCK offline lane)
        search_chain.py  cached search (Tavily → ddgs fallback)
      agents/
        discovery.py     rival identification (the live vertical slice)
      db/schema.sql      Postgres DDL (idempotent)
    static/index.html    render-proof UI
    tests/               contract + boundary suites

This is the Saturday 10:00 scaffold cloned from `poc_vertical_slice/`: the in-memory
job store becomes Dharvi's Postgres repository, `threading.Thread` became FastAPI
BackgroundTasks, and the one discovery agent grows into the full LangGraph graph.

Built during CodeClash 2026 by Team Argus. AI assistance declared in /DECLARATION.md.
