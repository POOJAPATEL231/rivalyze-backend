"""Orchestrator tests: stub agents only, no network, no real router. Covers
injected agents, additive reducers on every parallel-written field, boundary
validation with typed-empty substitution, the graph never raising when an agent
is killed, and validate's exactly-one strategist repair retry."""
import pytest

from app.core.orchestrator import (
    build_graph,
    discovery_node,
    gather_node,
    run_pipeline,
    strategist_node,
)
from app.models import (
    Competitor,
    CompetitiveReport,
    CompetitorSet,
    NewsSignals,
    ProductIntel,
    SentimentIntel,
    Swot,
    UnifiedSignals,
)


def collector():
    events: list[tuple[str, str]] = []
    return events, lambda agent, msg: events.append((agent, msg))


def stub_discovery(company, domain, emit):
    return CompetitorSet(competitors=[Competitor(name="Coda", category="direct", rationale="x")])


def stub_news(companies, emit):
    return [NewsSignals(competitor=c, items=[]) for c in companies]


def stub_product(companies, emit):
    return [ProductIntel(competitor=c) for c in companies]


def stub_review(companies, emit):
    return [SentimentIntel(competitor=c) for c in companies]


def stub_merge(state, emit):
    return {"unified": UnifiedSignals(low_signal_findings=list(state.get("low_signal_findings", [])))}


def good_report(company="", **overrides):
    defaults = dict(company=company, threat_level="MEDIUM", executive_summary="a real summary",
                     swot=Swot(), analysis_date="2026-07-04")
    defaults.update(overrides)
    return CompetitiveReport(**defaults)


def stub_strategist(unified, company, confidence_fn, emit):
    return good_report(company)


def make_agents(**overrides):
    agents = {
        "discovery": discovery_node(stub_discovery),
        "news": gather_node("news", stub_news),
        "product": gather_node("product", stub_product),
        "review": gather_node("review", stub_review),
        "merge": stub_merge,
        "strategist": strategist_node(stub_strategist, lambda s, a, c: 0.5),
    }
    agents.update(overrides)
    return agents


BASE_STATE = {"company": "Notion", "domain": "docs"}


def test_happy_path_produces_a_valid_report():
    _, emit = collector()
    result = run_pipeline(dict(BASE_STATE), make_agents(), emit)
    assert result["report"] is not None
    assert result["report"].company == "Notion"
    assert len(result["competitors"]) == 1
    assert result["news_results"] and result["product_results"] and result["review_results"]


def test_events_use_lane_canonical_names_reviews_is_plural():
    events, emit = collector()
    run_pipeline(dict(BASE_STATE), make_agents(), emit)
    lanes = {agent for agent, _ in events}
    assert "reviews" in lanes
    assert "review" not in lanes
    assert {"discovery", "news", "product", "strategist", "system"} <= lanes


def test_killed_news_agent_still_completes_with_low_signal_finding():
    def dead_news(companies, emit):
        raise RuntimeError("news agent killed")

    _, emit = collector()
    result = run_pipeline(dict(BASE_STATE), make_agents(news=gather_node("news", dead_news)), emit)

    assert result["report"] is not None
    assert result["news_results"] == []
    assert result["product_results"] and result["review_results"]
    assert any("news" in f and "failed" in f for f in result["low_signal_findings"])


def test_all_gather_agents_killed_report_still_produced():
    def dead(companies, emit):
        raise RuntimeError("dead")

    _, emit = collector()
    agents = make_agents(
        news=gather_node("news", dead),
        product=gather_node("product", dead),
        review=gather_node("review", dead),
    )
    result = run_pipeline(dict(BASE_STATE), agents, emit)
    assert result["news_results"] == result["product_results"] == result["review_results"] == []
    assert result["report"] is not None
    assert len(result["low_signal_findings"]) == 3


def test_every_agent_including_discovery_and_strategist_killed_never_raises():
    def dead(*a, **k):
        raise RuntimeError("everything is dead")

    _, emit = collector()
    agents = {
        "discovery": discovery_node(dead),
        "news": gather_node("news", dead),
        "product": gather_node("product", dead),
        "review": gather_node("review", dead),
        "merge": dead,
        "strategist": strategist_node(dead, lambda s, a, c: 0.5),
    }
    result = run_pipeline(dict(BASE_STATE), agents, emit)
    assert result["report"] is None
    assert result["competitors"] == []


def test_malformed_item_in_a_list_is_dropped_not_the_whole_branch():
    def half_bad_news(companies, emit):
        return [
            NewsSignals(competitor=companies[0], items=[]),
            {"not": "a NewsSignals at all"},
        ]

    events, emit = collector()
    result = run_pipeline(dict(BASE_STATE), make_agents(news=gather_node("news", half_bad_news)), emit)
    assert len(result["news_results"]) == 1
    assert any("dropped invalid" in msg for _, msg in events)


def test_node_returning_wrong_top_level_type_degrades_to_typed_empty():
    def wrong_shape_news(companies, emit):
        return "not even a list"

    _, emit = collector()
    result = run_pipeline(dict(BASE_STATE), make_agents(news=gather_node("news", lambda c, e: wrong_shape_news(c, e))), emit)
    assert result["news_results"] == []


def test_discovery_is_a_no_op_when_competitors_already_confirmed():
    calls = []

    def spy_discovery(company, domain, emit):
        calls.append(1)
        return CompetitorSet(competitors=[])

    preset = [Competitor(name="ClickUp", category="direct", rationale="pre-confirmed")]
    state = {**BASE_STATE, "competitors": preset}
    result = run_pipeline(state, make_agents(discovery=discovery_node(spy_discovery)), emit=lambda a, m: None)
    assert calls == []
    assert result["competitors"] == preset


def test_validate_repairs_an_invalid_report_with_exactly_one_retry():
    call_count = {"n": 0}

    def flaky_strategist(unified, company, confidence_fn, emit):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return good_report(company, executive_summary="")
        return good_report(company, executive_summary="a repaired, real summary")

    events, emit = collector()
    result = run_pipeline(dict(BASE_STATE), make_agents(strategist=strategist_node(flaky_strategist, lambda s, a, c: 0.5)), emit)

    assert call_count["n"] == 2
    assert result["report"] is not None
    assert result["report"].executive_summary == "a repaired, real summary"
    assert any("repair retry" in msg for _, msg in events)


def test_validate_degrades_to_report_none_after_failed_repair():
    def always_bad_strategist(unified, company, confidence_fn, emit):
        return good_report(company, executive_summary="")

    events, emit = collector()
    result = run_pipeline(dict(BASE_STATE), make_agents(strategist=strategist_node(always_bad_strategist, lambda s, a, c: 0.5)), emit)

    assert result["report"] is None
    assert any("degraded run" in f for f in result["low_signal_findings"])
    assert any("still failing" in msg for _, msg in events)


def test_validate_accepts_a_report_that_passes_sanity_first_try_no_retry():
    call_count = {"n": 0}

    def good_strategist(unified, company, confidence_fn, emit):
        call_count["n"] += 1
        return good_report(company)

    _, emit = collector()
    run_pipeline(dict(BASE_STATE), make_agents(strategist=strategist_node(good_strategist, lambda s, a, c: 0.5)), emit)
    assert call_count["n"] == 1


def test_reducer_fields_hold_one_entry_per_parallel_branch_not_duplicated():
    _, emit = collector()
    result = run_pipeline(dict(BASE_STATE), make_agents(), emit)
    assert len(result["news_results"]) == 1
    assert len(result["product_results"]) == 1
    assert len(result["review_results"]) == 1


def test_idea_only_state_resolves_company_before_discovery_runs():
    seen_company = {}

    def spy_discovery(company, domain, emit):
        seen_company["company"] = company
        seen_company["domain"] = domain
        return CompetitorSet(competitors=[])

    state = {"idea": "an AI scheduling tool for freelance barbers"}
    result = run_pipeline(state, make_agents(discovery=discovery_node(spy_discovery)), emit=lambda a, m: None)
    assert seen_company["company"]
    assert seen_company["domain"]
    assert result["report"] is not None


def test_build_graph_raises_on_missing_agent_key():
    incomplete = make_agents()
    del incomplete["merge"]
    with pytest.raises(ValueError, match="merge"):
        build_graph(incomplete, emit=lambda a, m: None)


def test_build_graph_succeeds_with_all_required_keys():
    graph = build_graph(make_agents(), emit=lambda a, m: None)
    assert graph is not None
