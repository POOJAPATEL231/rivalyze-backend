"""POST /analyze runs a FRESH analysis by default — a re-run must not silently
return the stored report (that showed stale data and blocked re-selecting rivals).
The instant stored report is opt-in via ?cached=true."""
from app.api import routes
from app.models import AnalyzeRequest, AnalyzeResponse, UserPublic


class _BG:
    def add_task(self, *a, **k):
        pass


# the handler is called directly (bypassing FastAPI DI), so pass the identity
# get_current_user would otherwise inject
_USER = UserPublic(user_id="00000000-0000-0000-0000-000000000000", email="u@example.com")


def test_default_runs_fresh_not_the_stored_report(monkeypatch):
    seen = {"find": False, "start": False}

    def fake_find(company):
        seen["find"] = True
        return "old-job-id"

    def fake_start(req, bg, user_id=None):
        seen["start"] = True
        return AnalyzeResponse(job_id="fresh-job-id", status="running_discovery")

    monkeypatch.setattr(routes.lifecycle, "find_completed", fake_find)
    monkeypatch.setattr(routes.lifecycle, "start_run", fake_start)
    req = AnalyzeRequest(company="Zomato", domain="Food Delivery")

    # default: fresh run, the stored-report lookup is never consulted
    resp = routes.analyze(req, _BG(), current_user=_USER, cached=False)
    assert resp.job_id == "fresh-job-id"
    assert seen["start"] and not seen["find"]

    # opt-in: cached=true serves the stored report instantly
    seen["start"] = False
    resp = routes.analyze(req, _BG(), current_user=_USER, cached=True)
    assert resp.job_id == "old-job-id"
    assert seen["find"] and not seen["start"]
