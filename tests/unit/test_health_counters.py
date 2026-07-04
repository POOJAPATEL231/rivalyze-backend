"""GET /api/v1/health's `counters` field (added for Module 4's --budget flag).

No real Redis needed: app.core.counters.counter_get degrades to 0 gracefully
when Redis is unavailable/unconfigured, so this runs everywhere, no DB/Redis
skip marker required.
"""
import os

os.environ.setdefault("MOCK_MODE", "1")  # must be set before app import

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

_BUDGETS_PATH = Path(__file__).resolve().parents[2] / "budgets.json"
_BUDGET_PROVIDERS = set(json.loads(_BUDGETS_PATH.read_text(encoding="utf-8")))


def test_health_exposes_counters_for_every_budgeted_provider():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "rivalyze"
    assert set(body["counters"]) == _BUDGET_PROVIDERS
    assert all(isinstance(v, int) for v in body["counters"].values())


def test_health_counters_default_to_zero_without_redis(monkeypatch):
    # Simulate "Redis unavailable" the same way counters.py itself does —
    # counter_get must degrade to 0, never raise, never break /health.
    from app.core import counters
    monkeypatch.setattr(counters, "_get_client", lambda: None)

    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert all(v == 0 for v in r.json()["counters"].values())
