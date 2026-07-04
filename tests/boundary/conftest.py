"""Boundary tests are hermetic unit tests of pipeline LOGIC — never the database.

Node-side code (merge, lifecycle) persists best-effort through the repository. To
keep these tests fast and DB-independent (and to never block on a configured-but-
unreachable Postgres from a local .env), stub every repository WRITE to a no-op.
Reads aren't stubbed — boundary tests don't call them.
"""
import pytest


@pytest.fixture(autouse=True)
def _stub_repository_writes(monkeypatch):
    try:
        from app.db import repository
    except Exception:
        return
    for name in ("save_evidence", "save_signal", "save_report", "save_competitors",
                 "replace_competitors", "append_events", "update_run_status",
                 "set_lane_stats", "finish_run", "fail_run"):
        monkeypatch.setattr(repository, name, lambda *a, **k: None, raising=False)
    yield
