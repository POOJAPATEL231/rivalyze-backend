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
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .api.auth_routes import router as auth_router
from .api.companies_routes import router as companies_router
from .api.history_routes import router as history_router
from .api.routes import router as api_router
from .core import config
from .core.ratelimit import limiter

logger = logging.getLogger("rivalyze")

@asynccontextmanager
async def lifespan(_: FastAPI):
    if not config.BEARER_TOKEN and (config.MOCK_MODE or config.AUTH_DISABLED):
        logger.warning(
            "AUTH OPEN: no BEARER_TOKEN set (MOCK_MODE/AUTH_DISABLED). "
            "Do NOT run this configuration in production."
        )
    yield


app = FastAPI(title="Rivalyze", version="0.1", lifespan=lifespan)

# rate limiting: register the shared limiter + the 429 handler (slowapi looks
# these up on app.state). Per-route caps live on the auth endpoints.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Auth is header-based (Bearer), never cookie-based, so credentials are not needed;
# keeping allow_credentials=False + pinned methods/headers is least-privilege and
# limits the blast radius if FRONTEND_ORIGIN is ever misconfigured.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[config.FRONTEND_ORIGIN],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    return resp


app.include_router(api_router)
app.include_router(auth_router)
app.include_router(history_router)
app.include_router(companies_router)

_INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
def ui() -> str:
    """Minimal UI proving the end-to-end render step (POC vertical slice)."""
    return _INDEX.read_text(encoding="utf-8")
