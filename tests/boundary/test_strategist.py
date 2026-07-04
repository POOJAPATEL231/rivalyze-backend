"""TC-B04 — the strategist owns the numbers and the citations, not the model.

Confidence is recomputed from cited evidence (model numbers discarded); unknown
ids are stripped from every item. A rec that cites ONLY unknown ids is KEPT (with
those ids stripped and a baseline confidence) rather than deleted — an empty
recommendations section is worse for the board than a concrete-but-uncited action,
and code still guarantees no unknown id ever reaches the output. The MOCK lane
exercises the full run() producing a valid report offline.
"""
from app.agents import strategist
from app.core.confidence import confidence
from app.models import Recommendation, UnifiedSignals


def _noop(agent, msg):
    pass


_INDEX = {
    "ev-a": {"agent": "news", "competitor": "X", "type": "news", "url": "https://u/1"},
    "ev-b": {"agent": "product", "competitor": "X", "type": "pricing", "url": "https://u/2"},
}


def test_rec_citing_only_unknown_evidence_is_kept_with_ids_stripped():
    recs = [Recommendation(action="a", rationale="r", confidence=0.9,
                           evidence_ids=["ev-missing"], claim_ref="rec:1")]
    kept = strategist._clean_cited(recs, _INDEX, confidence, _noop, kind="recommendation")
    assert len(kept) == 1
    assert kept[0].evidence_ids == []                      # bogus id stripped, not asserted
    assert kept[0].confidence == confidence(0, 0, 0)       # baseline, model's 0.9 discarded


def test_unknown_ids_stripped_and_confidence_recomputed():
    recs = [Recommendation(action="a", rationale="r", confidence=0.9,
                           evidence_ids=["ev-a", "ev-b", "ev-missing"], claim_ref="rec:1")]
    kept = strategist._clean_cited(recs, _INDEX, confidence, _noop, kind="recommendation")
    assert len(kept) == 1
    assert kept[0].evidence_ids == ["ev-a", "ev-b"]        # unknown removed
    # 2 distinct sources, 2 distinct agents, largest (competitor,type) cluster = 1
    assert kept[0].confidence == confidence(2, 1, 2)
    assert kept[0].confidence != 0.9                       # model number discarded


def test_rec_with_no_citations_is_kept_with_baseline_confidence():
    recs = [Recommendation(action="a", rationale="r", confidence=0.9,
                           evidence_ids=[], claim_ref="rec:1")]
    kept = strategist._clean_cited(recs, _INDEX, confidence, _noop, kind="recommendation")
    assert len(kept) == 1
    assert kept[0].confidence == confidence(0, 0, 0)       # 0.25 baseline


def test_h2h_cells_get_drawer_queryable_claim_ref():
    # evidence graph: Coda has pricing + review evidence; the h2h cells must be
    # linked to the claim_ref the drawer can query, chosen by the dimension.
    index = {
        "ev-1": {"competitor": "Coda", "type": "pricing", "claim_ref": "pricing:coda"},
        "ev-2": {"competitor": "Coda", "type": "review", "claim_ref": "review:coda"},
    }
    cbs = strategist._claim_refs_by_competitor(index)
    raw = [
        {"metric": "Pricing", "you": "us", "rivals": {"Coda": {"value": "$10"}}},
        {"metric": "Customer Sentiment", "you": "us", "rivals": {"Coda": {"value": "mixed"}}},
        {"metric": "Market Position", "you": "us", "rivals": {"Coda": {"value": "leader"}}},
    ]
    rows = strategist._coerce_h2h(raw, cbs)
    assert rows[0].rivals["Coda"].claim_ref == "pricing:coda"          # pricing dimension
    assert rows[1].rivals["Coda"].claim_ref == "review:coda"           # sentiment -> review
    # "Market Position" -> news, which has no evidence -> falls back to an available ref
    assert rows[2].rivals["Coda"].claim_ref in {"pricing:coda", "review:coda"}


def test_h2h_cell_without_evidence_stays_uncited():
    rows = strategist._coerce_h2h(
        [{"metric": "Pricing", "you": "us", "rivals": {"Ghost": {"value": "?"}}}],
        strategist._claim_refs_by_competitor({}))
    assert rows[0].rivals["Ghost"].claim_ref is None                   # never faked


def test_run_produces_valid_report_offline():
    # requires MOCK_MODE=1 (set by the CI/test invocation); the mock strategist
    # lane cites the evidence ids present in the prompt so a rec survives.
    unified = UnifiedSignals(
        signals=[],
        per_competitor={
            "ClickUp": {"pricing_tiers": ["Pro $12"], "evidence_ids": ["ev-a"]},
            strategist.EVIDENCE_INDEX_KEY: _INDEX,
        },
        low_signal_findings=["seeded finding"],
    )
    report = strategist.run(unified, "Notion", confidence, _noop)
    assert report.company == "Notion"                      # code-stamped
    assert report.analysis_date                            # code-stamped, non-empty
    assert report.executive_summary.strip()
    assert "seeded finding" in report.low_signal_findings  # carried from unified
    assert len(report.recommendations) <= 3
    for rec in report.recommendations:
        assert 0.05 <= rec.confidence <= 0.95
        assert all(i in _INDEX for i in rec.evidence_ids)  # only known ids survive
