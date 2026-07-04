"""Shared test fixtures.

By default rate limiting is DISABLED for tests: the auth/refresh/smoke suites
fire many requests from one client and would otherwise trip the limiter. The
dedicated rate-limit test re-enables it explicitly for its own assertions.
"""
import pytest


@pytest.fixture(autouse=True)
def _rate_limit_disabled():
    try:
        from app.core.ratelimit import limiter
    except Exception:
        yield
        return
    was = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = was
