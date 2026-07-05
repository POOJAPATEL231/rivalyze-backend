"""Shared test fixtures.

MOCK_MODE is pinned here, BEFORE any test module imports `app`: pytest imports
this conftest first, so the setdefault below wins over the `.env` value
(load_dotenv never overrides an existing env var). Without this, whichever test
file happened to be collected first decided the mode for the WHOLE process — a
file that imported `app` without setting MOCK_MODE (e.g. tests/agents/test_idea.py)
loaded `.env`'s MOCK_MODE=0, and every pipeline test afterwards silently made
REAL provider calls: nondeterministic suite failures + burned API credits.
Run a deliberate real-mode suite with:  MOCK_MODE=0 pytest ...

By default rate limiting is DISABLED for tests: the auth/refresh/smoke suites
fire many requests from one client and would otherwise trip the limiter. The
dedicated rate-limit test re-enables it explicitly for its own assertions.
"""
import os

os.environ.setdefault("MOCK_MODE", "1")  # must run before anything imports `app`

import pytest  # noqa: E402


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
