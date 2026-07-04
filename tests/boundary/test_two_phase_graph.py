"""The two compiled graphs, exercised offline (MOCK_MODE) with no database.

Covers the phase split (discovery-only vs analysis) and TC-N03 degradation: a
dead gathering branch never takes the analysis graph down — a valid report still
lands.
"""
from app.core import confidence, merge, orchestrator
from app.models import Competitor, CompetitiveReport


def _noop(agent, msg):
    pass


def _build_agents():
    from app.agents import discovery, news, product, review, strategist
    return {
        "discovery": orchestrator.discovery_node(discovery.run),
        "news": orchestrator.gather_node("news", news.run),
        "product": orchestrator.gather_node("product", product.run),
        "review": orchestrator.gather_node("review", review.run),
        "merge": merge.merge_node,
        "strategist": orchestrator.strategist_node(strategist.run, confidence.confidence),
    }


def test_discovery_graph_only_discovers():
    agents = _build_agents()
    final = orchestrator.run_discovery(
        {"company": "Notion", "domain": "workspace", "run_id": "r1"}, agents, _noop)
    assert "competitors" in final
    assert final.get("report") is None                     # discovery graph never runs analysis


def test_analysis_graph_produces_report_with_confirmed_rivals():
    agents = _build_agents()
    confirmed = [Competitor(name="ClickUp"), Competitor(name="Coda")]
    final = orchestrator.run_analysis(
        {"run_id": "r2", "company": "Notion", "competitors": confirmed}, agents, _noop)
    report = final["report"]
    assert isinstance(report, CompetitiveReport)
    rivals_in_h2h = {r for row in report.head_to_head for r in row.rivals}
    assert rivals_in_h2h <= {"ClickUp", "Coda"}            # only confirmed rivals analyzed
    assert report.executive_summary.strip()


def test_analysis_survives_a_dead_branch(monkeypatch):
    from app.agents import review

    def _boom(*a, **k):
        raise RuntimeError("review lane exploded")

    monkeypatch.setattr(review, "run", _boom)
    agents = _build_agents()                                # binds the patched review.run
    final = orchestrator.run_analysis(
        {"run_id": "r3", "company": "Notion", "competitors": [Competitor(name="ClickUp")]},
        agents, _noop)
    # graph still completes with a valid report; the failure is a typed finding
    assert isinstance(final["report"], CompetitiveReport)
    assert any("review" in f for f in final.get("low_signal_findings", []))
