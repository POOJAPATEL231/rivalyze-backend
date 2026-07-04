"""Unit tests for scripts/warmup.py — httpx calls mocked via respx, no real
server or database needed. time.sleep is patched to a no-op (autouse) so the
60s retry cooldown / 5s poll interval never actually block the test suite.
"""
import json

import httpx
import pytest
import respx

from scripts import warmup

BASE = "http://test"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(warmup.time, "sleep", lambda *_: None)


def _run_status(status: str, **overrides) -> dict:
    base = {
        "job_id": "job-1", "status": status, "current_stage": "done",
        "events": [], "result": None, "lane_stats": {"cache_hits": 2, "searches": 3},
        "run_id": "run-1", "error": None,
    }
    base.update(overrides)
    return base


# ================================== _poll ==================================
@respx.mock
def test_poll_returns_immediately_when_completed():
    respx.get(f"{BASE}/api/v1/runs/job-1").mock(return_value=httpx.Response(200, json=_run_status("completed")))
    with httpx.Client(base_url=BASE) as client:
        result = warmup._poll(client, "job-1")
    assert result["status"] == "completed"


@respx.mock
def test_poll_times_out_when_never_completes(monkeypatch):
    monkeypatch.setattr(warmup, "POLL_TIMEOUT_S", 0.02)
    respx.get(f"{BASE}/api/v1/runs/job-1").mock(return_value=httpx.Response(200, json=_run_status("running")))
    with httpx.Client(base_url=BASE) as client:
        result = warmup._poll(client, "job-1")
    assert result["status"] == "failed"
    assert "timeout" in result["error"]


# ================================ _run_one =================================
@respx.mock
def test_run_one_happy_path():
    respx.post(f"{BASE}/api/v1/analyze").mock(
        return_value=httpx.Response(200, json={"job_id": "job-1", "status": "queued"})
    )
    respx.get(f"{BASE}/api/v1/runs/job-1").mock(return_value=httpx.Response(200, json=_run_status("completed")))

    with httpx.Client(base_url=BASE) as client:
        entry = warmup._run_one(client, "Acme")

    assert entry == {
        "company": "Acme", "outcome": "completed", "attempt": 1,
        "duration_s": entry["duration_s"], "lane_stats": {"cache_hits": 2, "searches": 3},
        "job_id": "job-1", "run_id": "run-1",
    }


@respx.mock
def test_run_one_retries_once_then_succeeds():
    respx.post(f"{BASE}/api/v1/analyze").mock(
        side_effect=[
            httpx.Response(200, json={"job_id": "job-1", "status": "queued"}),
            httpx.Response(200, json={"job_id": "job-2", "status": "queued"}),
        ]
    )
    respx.get(f"{BASE}/api/v1/runs/job-1").mock(return_value=httpx.Response(200, json=_run_status("failed", error="boom")))
    respx.get(f"{BASE}/api/v1/runs/job-2").mock(return_value=httpx.Response(200, json=_run_status("completed", job_id="job-2")))

    with httpx.Client(base_url=BASE) as client:
        entry = warmup._run_one(client, "Acme")

    assert entry["outcome"] == "completed"
    assert entry["attempt"] == 2
    assert entry["job_id"] == "job-2"


@respx.mock
def test_run_one_fails_after_one_retry_and_stops():
    analyze_route = respx.post(f"{BASE}/api/v1/analyze").mock(
        side_effect=[
            httpx.Response(200, json={"job_id": "job-1", "status": "queued"}),
            httpx.Response(200, json={"job_id": "job-2", "status": "queued"}),
        ]
    )
    respx.get(f"{BASE}/api/v1/runs/job-1").mock(return_value=httpx.Response(200, json=_run_status("failed", error="boom")))
    respx.get(f"{BASE}/api/v1/runs/job-2").mock(return_value=httpx.Response(200, json=_run_status("failed", error="boom again")))

    with httpx.Client(base_url=BASE) as client:
        entry = warmup._run_one(client, "Acme")

    assert entry["outcome"] == "failed"
    assert entry["attempt"] == 2
    assert entry["error"] == "boom again"
    assert analyze_route.call_count == 2  # exactly one retry, never a third attempt


# ============================= _budget_exceeded =============================
@respx.mock
def test_budget_none_never_checks_health():
    route = respx.get(f"{BASE}/api/v1/health")
    with httpx.Client(base_url=BASE) as client:
        assert warmup._budget_exceeded(client, None) is False
    assert not route.called


@respx.mock
def test_budget_under_limit_is_false():
    respx.get(f"{BASE}/api/v1/health").mock(return_value=httpx.Response(200, json={"counters": {"tavily": 10}}))
    with httpx.Client(base_url=BASE) as client:
        assert warmup._budget_exceeded(client, 500) is False


@respx.mock
def test_budget_at_or_over_limit_is_true():
    respx.get(f"{BASE}/api/v1/health").mock(return_value=httpx.Response(200, json={"counters": {"tavily": 500}}))
    with httpx.Client(base_url=BASE) as client:
        assert warmup._budget_exceeded(client, 500) is True


@respx.mock
def test_budget_check_health_error_degrades_to_false():
    respx.get(f"{BASE}/api/v1/health").mock(return_value=httpx.Response(500))
    with httpx.Client(base_url=BASE) as client:
        assert warmup._budget_exceeded(client, 1) is False


# ================================== main ===================================
@respx.mock
def test_main_writes_manifest_and_returns_zero_on_success(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(["Acme"]), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"

    respx.post(f"{BASE}/api/v1/analyze").mock(return_value=httpx.Response(200, json={"job_id": "job-1", "status": "queued"}))
    respx.get(f"{BASE}/api/v1/runs/job-1").mock(return_value=httpx.Response(200, json=_run_status("completed")))

    code = warmup.main(["--base-url", BASE, "--seed", str(seed), "--manifest", str(manifest_path)])

    assert code == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["started_at"] and manifest["finished_at"]
    assert len(manifest["entries"]) == 1
    assert manifest["entries"][0]["outcome"] == "completed"
    assert manifest["entries"][0]["company"] == "Acme"


@respx.mock
def test_main_returns_nonzero_when_any_company_fails(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(["Acme"]), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"

    respx.post(f"{BASE}/api/v1/analyze").mock(return_value=httpx.Response(200, json={"job_id": "job-1", "status": "queued"}))
    respx.get(f"{BASE}/api/v1/runs/job-1").mock(return_value=httpx.Response(200, json=_run_status("failed", error="boom")))

    code = warmup.main(["--base-url", BASE, "--seed", str(seed), "--manifest", str(manifest_path)])

    assert code == 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["entries"][0]["outcome"] == "failed"


@respx.mock
def test_main_stops_before_next_company_once_budget_exceeded(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(["Acme", "Beta"]), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"

    respx.get(f"{BASE}/api/v1/health").mock(
        side_effect=[
            httpx.Response(200, json={"counters": {"tavily": 10}}),   # checked before Acme: under budget
            httpx.Response(200, json={"counters": {"tavily": 999}}),  # checked before Beta: over budget
        ]
    )
    analyze_route = respx.post(f"{BASE}/api/v1/analyze").mock(
        return_value=httpx.Response(200, json={"job_id": "job-1", "status": "queued"})
    )
    respx.get(f"{BASE}/api/v1/runs/job-1").mock(return_value=httpx.Response(200, json=_run_status("completed")))

    code = warmup.main(["--base-url", BASE, "--seed", str(seed), "--manifest", str(manifest_path), "--budget", "500"])

    assert code == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["stopped_early"] is True
    assert len(manifest["entries"]) == 1  # only Acme ran
    assert analyze_route.call_count == 1  # Beta's /analyze was never called
