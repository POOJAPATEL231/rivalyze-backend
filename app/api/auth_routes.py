"""Auth endpoints — email/password signup + login, JWT access tokens, rotating
refresh tokens, and /me.

  POST /api/v1/auth/signup   {email, password}   -> 201 {access_token, refresh_token, token_type}
  POST /api/v1/auth/login    {email, password}   -> 200 {access_token, refresh_token, token_type}
  POST /api/v1/auth/refresh  {refresh_token}     -> 200 {access_token, refresh_token, ...}  (rotates)
  POST /api/v1/auth/logout   {refresh_token}     -> 204  (revokes)
  GET  /api/v1/auth/me       (Bearer JWT)        -> {user_id, email}

Token model:
  - access_token: short-lived JWT, stateless, stored in NO table, sent as Bearer.
  - refresh_token: long-lived opaque random string. Only its SHA-256 hash is
    stored (refresh_tokens table). Each use ROTATES it (old one revoked, new one
    issued). Presenting an already-revoked token is treated as reuse/theft and
    revokes every refresh token for that user.

Security posture (unchanged from before): bcrypt password hashes, generic login
error + dummy verify (no user enumeration / timing leak), normalized emails.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..core import config, refresh_store, security, user_store
from ..core.auth import get_current_user
from ..core.ratelimit import limiter
from ..models import (
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    TokenResponse,
    UserPublic,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_UNAUTH = {"WWW-Authenticate": "Bearer"}


def _issue_tokens(user_id: str, email: str) -> TokenResponse:
    """Mint a fresh access+refresh pair and persist the refresh token's hash."""
    access = security.create_access_token(user_id, email)
    raw_refresh = security.generate_refresh_token()
    refresh_store.store(user_id, security.hash_refresh_token(raw_refresh),
                        security.refresh_expiry())
    return TokenResponse(access_token=access, refresh_token=raw_refresh)


@router.post("/signup", response_model=TokenResponse, status_code=201)
@limiter.limit(config.AUTH_RATELIMIT_SIGNUP)
def signup(request: Request, req: SignupRequest) -> TokenResponse:
    email = req.email.lower().strip()
    # fast path: friendly 409 without hashing when the email obviously exists
    if user_store.get_user_by_email(email) is not None:
        raise HTTPException(status_code=409, detail="email already registered")
    try:
        user = user_store.create_user(email, security.hash_password(req.password))
    except user_store.EmailAlreadyExistsError:
        # lost the race with a concurrent signup — same 409, never a 500 (TOCTOU)
        raise HTTPException(status_code=409, detail="email already registered")
    return _issue_tokens(user["id"], email)


@router.post("/login", response_model=TokenResponse)
@limiter.limit(config.AUTH_RATELIMIT_LOGIN)
def login(request: Request, req: LoginRequest) -> TokenResponse:
    email = req.email.lower().strip()
    user = user_store.get_user_by_email(email)
    if user is None:
        security.verify_password(req.password, security.DUMMY_HASH)  # constant-ish timing
        raise HTTPException(status_code=401, detail="invalid email or password")
    if not security.verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid email or password")
    return _issue_tokens(user["id"], email)


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(config.AUTH_RATELIMIT_REFRESH)
def refresh(request: Request, req: RefreshRequest) -> TokenResponse:
    token_hash = security.hash_refresh_token(req.refresh_token)
    rec = refresh_store.get(token_hash)
    if rec is None:
        raise HTTPException(status_code=401, detail="invalid refresh token", headers=_UNAUTH)
    if rec["revoked"]:
        # a revoked token was replayed -> assume theft, kill the whole family
        refresh_store.revoke_all_for_user(rec["user_id"])
        raise HTTPException(status_code=401, detail="refresh token reuse detected",
                            headers=_UNAUTH)
    if security.refresh_is_expired(rec["expires_at"]):
        raise HTTPException(status_code=401, detail="refresh token expired", headers=_UNAUTH)

    user = user_store.get_user_by_id(rec["user_id"])
    if user is None:
        raise HTTPException(status_code=401, detail="invalid refresh token", headers=_UNAUTH)

    refresh_store.revoke(token_hash)              # rotate: old token is now dead
    return _issue_tokens(user["id"], user["email"])


@router.post("/logout", status_code=204)
def logout(req: RefreshRequest) -> Response:
    # idempotent: revoking an unknown/already-revoked token is a no-op 204
    refresh_store.revoke(security.hash_refresh_token(req.refresh_token))
    return Response(status_code=204)


@router.get("/me", response_model=UserPublic)
def me(current_user: UserPublic = Depends(get_current_user)) -> UserPublic:
    return current_user
