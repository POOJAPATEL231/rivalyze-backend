"""TC-B01 — the merge code node fuses agent outputs into a cited evidence graph
and NEVER raises on a malformed item (it becomes a low_signal finding instead)."""
from app.core import merge
from app.core.merge import EVIDENCE_INDEX_KEY
from app.models import NewsItem, NewsSignals, ProductIntel, SentimentIntel


def _noop(agent, msg):
    pass


def test_merge_builds_evidence_signals_and_rollups():
    state = {
        "run_id": "run-1",
        "news_results": [NewsSignals(competitor="ClickUp", items=[
            NewsItem(event="Series C", impact="war chest", source_url="https://x.com/a", date="2026-06-01")])],
        "product_results": [ProductIntel(competitor="ClickUp", pricing_tiers=["Pro $12/seat"],
                                         sources=["https://x.com/pricing"])],
        "review_results": [SentimentIntel(competitor="ClickUp", top_complaints=["slow sync"],
                                          overall_sentiment="NEGATIVE", sources=["https://x.com/reviews"])],
    }
    out = merge.merge_node(state, _noop)
    unified = out["unified"]

    # one signal per agent finding
    assert {s.agent for s in unified.signals} == {"news", "product", "review"}
    # every signal carries at least one evidence id
    assert all(s.evidence_ids for s in unified.signals)

    index = unified.per_competitor[EVIDENCE_INDEX_KEY]
    assert len(index) == 3                                  # 3 sources -> 3 evidence rows
    # rollup carries the competitor's evidence ids and its intel
    roll = unified.per_competitor["ClickUp"]
    assert roll["pricing_tiers"] == ["Pro $12/seat"]
    assert roll["sentiment"] == "NEGATIVE"
    assert set(roll["evidence_ids"]) <= set(index)


def test_malformed_item_becomes_finding_never_raises():
    state = {"run_id": "run-2",
             "news_results": [{"totally": "invalid"}],   # not a NewsSignals
             "product_results": [], "review_results": []}
    out = merge.merge_node(state, _noop)                  # must not raise
    assert out["unified"].low_signal_findings             # recorded as a finding


def test_low_signal_agents_are_flagged():
    state = {"run_id": "run-3",
             "news_results": [NewsSignals(competitor="Coda", items=[], low_signal=True)],
             "product_results": [], "review_results": []}
    out = merge.merge_node(state, _noop)
    assert any("Coda" in f for f in out["unified"].low_signal_findings)
