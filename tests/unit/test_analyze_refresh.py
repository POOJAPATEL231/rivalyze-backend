"""POST /analyze?refresh=true must bypass the persistence-first shortcut so a user
who first analyzed 2 rivals can re-run and re-select (e.g. 4). Default (no refresh)
still serves the cached report instantly."""
from app.api import routes
from app.models import AnalyzeRequest, AnalyzeResponse


class _BG:
    def add_task(self, *a, **k):
        pass


def test_refresh_flag_controls_persistence_shortcut(monkeypatch):
    seen = {"find": False, "start": False}

    def fake_find(company):
        seen["find"] = True
        return "old-job-id"

    def fake_start(req, bg):
        seen["start"] = True
        return AnalyzeResponse(job_id="fresh-job-id", status="running_discovery")

    monkeypatch.setattr(routes.lifecycle, "find_completed", fake_find)
    monkeypatch.setattr(routes.lifecycle, "start_run", fake_start)

    req = AnalyzeRequest(company="Zomato", domain="Food Delivery")

    # default: serve the cached report, no fresh run
    resp = routes.analyze(req, _BG(), refresh=False)
    assert resp.job_id == "old-job-id"
    assert seen["find"] and not seen["start"]

    # refresh: skip the shortcut entirely, run a fresh two-phase pipeline
    seen["find"] = False
    resp = routes.analyze(req, _BG(), refresh=True)
    assert resp.job_id == "fresh-job-id"
    assert seen["start"] and not seen["find"]
