"""Rivalyze backend — FastAPI application entry.

Owns app assembly only: CORS locked to FRONTEND_ORIGIN, the /api/v1 router
include, and the minimal UI that proves the render step. Business logic lives
in the routers and core modules. Additional pod routers are included here as
they land (evidence, history/export/reports, stretch documents/chat).

Run:
  pip install -r requirements.txt
  MOCK_MODE=1 uvicorn app.main:app --port 8000     # offline, zero keys
  # PowerShell: $env:MOCK_MODE="1"; uvicorn app.main:app --port 8000
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .api.auth_routes import router as auth_router
from .api.routes import router as api_router
from .core import config
from .core.ratelimit import limiter

app = FastAPI(title="Rivalyze", version="0.1")

# rate limiting: register the shared limiter + the 429 handler (slowapi looks
# these up on app.state). Per-route caps live on the auth endpoints.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[config.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(auth_router)

_INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
def ui() -> str:
    """Minimal UI proving the end-to-end render step (POC vertical slice)."""
    return _INDEX.read_text(encoding="utf-8")
