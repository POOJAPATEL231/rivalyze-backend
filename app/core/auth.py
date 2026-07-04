"""Auth dependencies.

Two levels, deliberately separate:

  require_token      — service-level gate on the /api/v1 contract routes.
                       Accepts EITHER the static env BEARER_TOKEN (compared in
                       constant time) OR a valid user JWT.

  get_current_user   — strict: identifies the caller. Always requires a valid,
                       unexpired user JWT whose subject maps to a known user.
                       Used by routes that act as a specific user (e.g. /me).

Fail-closed posture: an empty BEARER_TOKEN only serves the surface OPEN when the
run is explicitly offline/dev (MOCK_MODE) or the operator opted out with
AUTH_DISABLED=1. In any other configuration a missing credential returns 503
rather than silently disabling auth — so a deploy that forgets to inject the
secret refuses to serve a credit-spending, data-returning endpoint open.

401s carry `WWW-Authenticate: Bearer` per RFC 6750. Errors are generic — they
never reveal whether a token was absent, malformed, expired, or unknown. Token
compares are constant-time to avoid leaking the secret via response timing.
"""
import secrets
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..models import UserPublic
from . import config, security, user_store

_UNAUTH = {"WWW-Authenticate": "Bearer"}

# Registered as an OpenAPI security scheme so Swagger shows a proper "Authorize"
# button (and reliably attaches `Authorization: Bearer <token>`), instead of a
# raw header text box. auto_error=False keeps OUR fail-closed 503/401 logic in
# control — the scheme just parses the header, it never rejects on its own.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Static BEARER_TOKEN or a user access-token (JWT). Paste the token "
                "only — Swagger adds the 'Bearer ' prefix.",
)


def require_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> None:
    token = credentials.credentials if credentials else None

    # 1) static service token (constant-time compare — no early-exit leak)
    if config.BEARER_TOKEN and token and secrets.compare_digest(token, config.BEARER_TOKEN):
        return

    # 2) a valid user JWT is also accepted on contract routes
    if token:
        try:
            security.decode_access_token(token)
            return
        except Exception:
            pass  # fall through

    # 3) no valid credential presented
    if not config.BEARER_TOKEN:
        # Open ONLY when explicitly offline/dev; otherwise fail CLOSED so a
        # misconfigured deploy never serves the surface unauthenticated.
        if config.MOCK_MODE or config.AUTH_DISABLED:
            return
        raise HTTPException(status_code=503, detail="server auth is not configured",
                            headers=_UNAUTH)

    raise HTTPException(status_code=401, detail="invalid or missing bearer token",
                        headers=_UNAUTH)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> UserPublic:
    token = credentials.credentials if credentials else None
    if not token:
        raise HTTPException(status_code=401, detail="authentication required",
                            headers=_UNAUTH)
    try:
        payload = security.decode_access_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="invalid or expired token",
                            headers=_UNAUTH)

    user = user_store.get_user_by_id(payload.get("sub", ""))
    if user is None:
        raise HTTPException(status_code=401, detail="invalid or expired token",
                            headers=_UNAUTH)
    return UserPublic(user_id=user["id"], email=user["email"])
