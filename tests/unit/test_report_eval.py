"""Report-quality evaluator (adopted from rivalyze-dev): scores a report via the
hardened router, clamps to 0-10, and NEVER raises (best-effort)."""
from app.core import report_eval

REPORT = {"executive_summary": "x", "swot": {"strengths": ["a"]},
          "sentiment": {}, "head_to_head": [], "opportunities": [], "recommendations": []}


def test_scores_a_report_in_mock_mode():
    out = report_eval.evaluate(REPORT, "Acme", lambda a, m: None)   # MOCK lane
    assert out is not None
    assert set(out) == {"completeness", "accuracy", "strategic_value",
                        "actionability", "overall_score"}
    assert all(0.0 <= v <= 10.0 for v in out.values())


def test_empty_report_returns_none():
    assert report_eval.evaluate({}, "Acme", lambda a, m: None) is None


def test_never_raises_on_llm_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("all lanes exhausted")
    monkeypatch.setattr(report_eval, "complete", boom)
    assert report_eval.evaluate(REPORT, "Acme", lambda a, m: None) is None   # None, not an exception


def test_clamps_out_of_range_scores(monkeypatch):
    class _S:
        def model_dump(self):
            return {"completeness": 99, "accuracy": -3, "strategic_value": 8,
                    "actionability": 7, "overall_score": 7.5}
    monkeypatch.setattr(report_eval, "complete", lambda *a, **k: (_S(), "mock"))
    out = report_eval.evaluate(REPORT, "Acme", lambda a, m: None)
    assert out["completeness"] == 10.0 and out["accuracy"] == 0.0    # clamped to [0,10]
