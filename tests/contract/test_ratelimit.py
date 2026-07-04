"""Rate limiting on auth endpoints — online brute-force / credential-stuffing.

The autouse conftest fixture disables the limiter for every other test; here we
re-enable it and prove the cap engages. Uses /refresh (no bcrypt, no writes):
invalid tokens 401 until the per-IP limit trips, then 429.

Run:  MOCK_MODE=1 python -m pytest tests/contract/test_ratelimit.py -q
"""
import os

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-prod-min-32-bytes-long-xyz")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.ratelimit import limiter  # noqa: E402
from app.db import connection  # noqa: E402
from app.main import app  # noqa: E402

pytestmark = pytest.mark.skipif(not connection.is_enabled(),
                                reason="requires a database (set DATABASE_URL or PG*)")

client = TestClient(app)


def test_refresh_endpoint_is_rate_limited():
    limiter.enabled = True
    try:
        codes = [client.post("/api/v1/auth/refresh", json={"refresh_token": "x"}).status_code
                 for _ in range(15)]
    finally:
        limiter.enabled = False

    assert 401 in codes, f"early attempts should pass through as 401, got {codes}"
    assert 429 in codes, f"expected the limiter to trip (429), got {codes}"
    # default cap is 10/minute → roughly 10 allowed then 429s
    assert codes.count(401) <= 11
