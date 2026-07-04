"""Auth dependencies.

Two levels, deliberately separate:

  require_token      — service-level gate on the /api/v1 contract routes.
                       Accepts EITHER the static env BEARER_TOKEN (compared in
                       constant time) OR a valid user JWT. Stays dev-open only
                       when no BEARER_TOKEN is configured, so the offline MOCK
                       slice keeps working; once a token is set the gate is real.

  get_current_user   — strict: identifies the caller. Always requires a valid,
                       unexpired user JWT whose subject maps to a known user.
                       Used by routes that act as a specific user (e.g. /me).

401s carry `WWW-Authenticate: Bearer` per RFC 6750. Errors are generic — they
never reveal whether a token was absent, malformed, expired, or unknown.
"""
import secrets
from typing import Optional

from fastapi import Header, HTTPException

from ..models import UserPublic
from . import config, security, user_store

_UNAUTH = {"WWW-Authenticate": "Bearer"}


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):].strip()
        return token or None
    return None


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    token = _bearer(authorization)

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

    # 3) dev-open ONLY when there is no service token to check against
    if not config.BEARER_TOKEN:
        return

    raise HTTPException(status_code=401, detail="invalid or missing bearer token",
                        headers=_UNAUTH)


def get_current_user(authorization: Optional[str] = Header(default=None)) -> UserPublic:
    token = _bearer(authorization)
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
