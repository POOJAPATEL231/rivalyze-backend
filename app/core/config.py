"""Runtime configuration — the one place that reads the environment.

Read once at import (after app/__init__.py has loaded .env). Downstream
modules import the resolved values from here instead of scattering os.getenv
calls. Provider API keys stay OUT of this module on purpose: the llm_router
and search_chain read their own keys lazily so a missing lane is skipped, not
a startup crash.
"""
import logging
import os
import secrets

log = logging.getLogger("rivalyze.config")


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) == "1"


# --- run mode ---
MOCK_MODE: bool = _flag("MOCK_MODE")        # 1 = deterministic offline lanes, zero keys
DEMO_RESERVE: bool = _flag("DEMO_RESERVE")  # 1 = hold budget back for the live demo

# --- API surface ---
BEARER_TOKEN: str = os.getenv("BEARER_TOKEN", "")            # empty = auth open (dev)
# Allowed browser origins for CORS. Default "*" = allow any origin, so the UI
# works from Chrome without pinning a URL. Can be narrowed later to a
# comma-separated allowlist, e.g. "http://localhost:5173,https://rivalyze.example"
# (no trailing slashes — the browser's Origin header has no path).
FRONTEND_ORIGINS: list[str] = [
    o.strip().rstrip("/") for o in os.getenv("FRONTEND_ORIGIN", "*").split(",") if o.strip()
]
# kept for any single-origin references
FRONTEND_ORIGIN: str = FRONTEND_ORIGINS[0] if FRONTEND_ORIGINS else "*"

# --- persistence (wired in when Dharvi's repository lands) ---
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
REDIS_URL: str = os.getenv("REDIS_URL", "")

# --- JWT user auth ---
# Secret MUST come from the environment in any shared/deployed run. When it is
# absent (local/MOCK dev) we mint a random per-process secret so tokens are
# still unforgeable — but they do NOT survive a restart, and every worker gets
# its own, so never rely on the ephemeral path in production. NEVER hardcode a
# fallback secret: a known signing key = anyone can forge any user's token.
JWT_SECRET: str = os.getenv("JWT_SECRET") or secrets.token_urlsafe(32)
JWT_SECRET_IS_EPHEMERAL: bool = "JWT_SECRET" not in os.environ
JWT_ALGORITHM: str = "HS256"
JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))
# Refresh tokens are long-lived and revocable (stored hashed in refresh_tokens).
REFRESH_TOKEN_EXPIRE_DAYS: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

# --- rate limiting (auth endpoints, keyed by client IP) ---
# Throttles online brute-force / credential-stuffing that bcrypt alone can't stop.
# Redis-backed when REDIS_URL is set (limits survive restarts + are shared across
# workers); otherwise in-process memory. Disable in tests via RATE_LIMIT_ENABLED=0.
RATE_LIMIT_ENABLED: bool = _flag("RATE_LIMIT_ENABLED", "1")
RATE_LIMIT_STORAGE_URI: str = os.getenv("RATELIMIT_STORAGE_URI") or (REDIS_URL or "memory://")
AUTH_RATELIMIT_SIGNUP: str = os.getenv("RATELIMIT_SIGNUP", "5/minute")
AUTH_RATELIMIT_LOGIN: str = os.getenv("RATELIMIT_LOGIN", "10/minute")
AUTH_RATELIMIT_REFRESH: str = os.getenv("RATELIMIT_REFRESH", "10/minute")

if JWT_SECRET_IS_EPHEMERAL:
    log.warning(
        "JWT_SECRET not set — using an ephemeral per-process secret. Tokens will "
        "not survive a restart. Set JWT_SECRET in .env before deploying."
    )
