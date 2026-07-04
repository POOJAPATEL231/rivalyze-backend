"""Shared rate limiter for the auth endpoints.

bcrypt makes OFFLINE cracking slow, but does nothing against ONLINE
brute-force / credential-stuffing — those need a request throttle. This limiter
is keyed by client IP (get_remote_address) and backed by Redis when REDIS_URL is
set, so counts survive restarts and are shared across workers; otherwise it uses
in-process memory (fine for a single dev process, resets on restart).

Wired in app/main.py (app.state.limiter + the RateLimitExceeded handler) and
applied per-route in app/api/auth_routes.py. Toggle with RATE_LIMIT_ENABLED.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

from . import config

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=config.RATE_LIMIT_STORAGE_URI,
    default_limits=[],  # no global limit; only the decorated auth routes are capped
)
# set after construction so it works regardless of the installed slowapi version
limiter.enabled = config.RATE_LIMIT_ENABLED
