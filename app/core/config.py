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


def _int_env(name: str, default: int) -> int:
    """Parse an int env var, falling back to `default` on a blank/garbage value.

    A bad value (e.g. JWT_EXPIRE_MINUTES="" or "60m") must NOT crash config
    import — that would take down the whole app at startup over one env typo.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        log.warning("config: %s=%r is not an integer — using default %d", name, raw, default)
        return default


def _redis_storage_uri(redis_url: str) -> str:
    """Rate-limiter storage. Use REDIS_URL only if it has a real scheme; a
    scheme-less value (a bare host:port) would crash the limits backend, so fall
    back to in-process memory rather than take down startup."""
    if redis_url.startswith(("redis://", "rediss://", "unix://")):
        return redis_url
    return "memory://"


# --- run mode ---
MOCK_MODE: bool = _flag("MOCK_MODE")        # 1 = deterministic offline lanes, zero keys
DEMO_RESERVE: bool = _flag("DEMO_RESERVE")  # 1 = hold budget back for the live demo
AUTH_DISABLED: bool = _flag("AUTH_DISABLED")  # 1 = explicit dev opt-out for auth (never in prod)

# --- API surface ---
# Empty token is only allowed to serve open when MOCK_MODE or AUTH_DISABLED is set;
# otherwise auth fails CLOSED (see core/auth.py) so a misconfigured deploy can't run open.
BEARER_TOKEN: str = os.getenv("BEARER_TOKEN", "")
FRONTEND_ORIGIN: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")

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
JWT_EXPIRE_MINUTES: int = _int_env("JWT_EXPIRE_MINUTES", 60)
# Refresh tokens are long-lived and revocable (stored hashed in refresh_tokens).
REFRESH_TOKEN_EXPIRE_DAYS: int = _int_env("REFRESH_TOKEN_EXPIRE_DAYS", 30)

# --- rate limiting (auth endpoints, keyed by client IP) ---
# Throttles online brute-force / credential-stuffing that bcrypt alone can't stop.
# Redis-backed when REDIS_URL is set (limits survive restarts + are shared across
# workers); otherwise in-process memory. Disable in tests via RATE_LIMIT_ENABLED=0.
RATE_LIMIT_ENABLED: bool = _flag("RATE_LIMIT_ENABLED", "1")
RATE_LIMIT_STORAGE_URI: str = os.getenv("RATELIMIT_STORAGE_URI") or _redis_storage_uri(REDIS_URL)
AUTH_RATELIMIT_SIGNUP: str = os.getenv("RATELIMIT_SIGNUP", "5/minute")
AUTH_RATELIMIT_LOGIN: str = os.getenv("RATELIMIT_LOGIN", "10/minute")
AUTH_RATELIMIT_REFRESH: str = os.getenv("RATELIMIT_REFRESH", "10/minute")

if JWT_SECRET_IS_EPHEMERAL:
    log.warning(
        "JWT_SECRET not set — using an ephemeral per-process secret. Tokens will "
        "not survive a restart. Set JWT_SECRET in .env before deploying."
    )
