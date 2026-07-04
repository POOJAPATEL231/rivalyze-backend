"""Contract smoke test — the MOCK slice runs end-to-end with zero keys.

Mirrors the C-series contract checks: /health is open, POST /analyze returns a
queued job_id, polling /runs reaches completed with a typed self-excluding
result, unknown jobs 404, and an empty request 422.

Run:  MOCK_MODE=1 python -m pytest tests/contract/test_smoke.py -q
"""
import os

os.environ.setdefault("MOCK_MODE", "1")  # must be set before app import

import time  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health_is_open_and_shaped():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "rivalyze"}


def test_analyze_then_poll_completes():
    r = client.post("/api/v1/analyze",
                    json={"company": "Notion", "domain": "connected workspace software"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    job_id = body["job_id"]

    status = {}
    for _ in range(50):
        status = client.get(f"/api/v1/runs/{job_id}").json()
        if status["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)

    assert status["status"] == "completed"
    assert status["run_id"]
    names = [c["name"].lower() for c in status["result"]["competitors"]]
    assert names, "expected at least one competitor"
    assert "notion" not in names, "input company must be self-excluded"


def test_unknown_job_returns_404():
    assert client.get("/api/v1/runs/does-not-exist").status_code == 404


def test_empty_request_returns_422():
    r = client.post("/api/v1/analyze", json={"company": "", "idea": None})
    assert r.status_code == 422
    assert r.json()["detail"] == "provide a company or an idea"
