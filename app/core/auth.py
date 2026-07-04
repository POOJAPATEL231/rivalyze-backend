"""Bearer-token auth dependency — a single shared secret from env BEARER_TOKEN.

Applied to every /api/v1 route EXCEPT /health (which is intentionally open so
load balancers and QA can probe it). Dev ergonomics: when BEARER_TOKEN is unset
the dependency is a no-op, so the bundled MOCK-mode UI and offline smoke tests
run without ceremony. Set BEARER_TOKEN in .env to lock the surface.
"""
from typing import Optional

from fastapi import Header, HTTPException

from . import config


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    if not config.BEARER_TOKEN:
        return  # no token configured -> auth open (local/MOCK dev)
    if authorization != f"Bearer {config.BEARER_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
