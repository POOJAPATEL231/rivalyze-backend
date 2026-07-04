"""Strategist agent — Module 3 (Sheel, Virat pairs). The quality-critical
synthesis step: turns merged, evidence-backed signals into the report the
judges see.

Deliberate exception to the usual "never raise" rule: on total lane
exhaustion this function DOES raise. Per the module reference, that failure
is handled specially by the validate node's cross-lane repair retry, not by
the generic per-node boundary wrapper — so it must propagate, not be
swallowed here.
"""
from datetime import date

from ..models import (
    CompetitiveReport, Opportunity, Recommendation, Signal, Swot,
    SentimentScore, UnifiedSignals,
)
from ..core import llm_router

MAX_RECOMMENDATIONS = 3


def run(unified: UnifiedSignals, company: str, confidence_fn, emit) -> CompetitiveReport:
    valid_evidence_ids = _collect_valid_evidence_ids(unified.signals)

    prompt = _build_prompt(unified, company)
    report, lane = llm_router.complete("reason", prompt, CompetitiveReport, emit)
    emit("strategist", f"draft synthesized via {lane}")

    # System-authored fields rule: the model never controls these.
    report.company = company
    report.analysis_date = date.today().isoformat()
    report.low_signal_findings = list(unified.low_signal_findings)

    report.recommendations = _rebuild_recommendations(
        report.recommendations, valid_evidence_ids, unified.signals, confidence_fn, emit
    )
    report.opportunities = _rebuild_opportunities(
        report.opportunities, valid_evidence_ids, emit
    )

    emit("strategist",
         f"{len(report.recommendations)} recommendations, "
         f"{len(report.opportunities)} opportunities after evidence filtering")
    return report


def _build_prompt(unified: UnifiedSignals, company: str) -> str:
    rollups = _serialize_rollups(unified.per_competitor)
    return f"""You are a competitive strategist writing a report about {company}.

THREAT RUBRIC: most markets are MEDIUM. Only mark HIGH or CRITICAL when the
evidence below explicitly shows direct, immediate competitive pressure
(e.g. a rival launching an equivalent product at lower price, or funding
that materially changes their ability to compete). Do not default upward.

EVIDENCE-CITED ROLLUPS (per competitor, each item already has evidence_ids
you must reuse verbatim — never invent a new evidence_id, never cite an id
that is not listed here):
{rollups}

LOW-SIGNAL FINDINGS (context only, do not restate as recommendations):
{unified.low_signal_findings}

Write a CompetitiveReport: executive_summary, threat_level, swot
(strengths/weaknesses/opportunities/threats as short strings), sentiment
per competitor (score 0-1, label), opportunities (text + evidence_ids +
claim_ref), recommendations (action, rationale, confidence 0-1 as your
best guess — it will be recomputed, evidence_ids, claim_ref). Maximum 3
recommendations. Every evidence_ids entry MUST come from the rollups above.

Return bare JSON matching the CompetitiveReport shape exactly."""


def _serialize_rollups(per_competitor: dict) -> str:
    lines = []
    for competitor, rollup in per_competitor.items():
        lines.append(f"- {competitor}: {rollup}")
    return "\n".join(lines) if lines else "(no rollups — treat as low signal)"


def _collect_valid_evidence_ids(signals: list[Signal]) -> set[str]:
    ids: set[str] = set()
    for s in signals:
        ids.update(s.evidence_ids)
    return ids


def _rebuild_recommendations(
    recommendations: list[Recommendation],
    valid_evidence_ids: set[str],
    signals: list[Signal],
    confidence_fn,
    emit,
) -> list[Recommendation]:
    kept = []
    for rec in recommendations:
        unknown = [eid for eid in rec.evidence_ids if eid not in valid_evidence_ids]
        if unknown:
            emit("strategist",
                 f"dropped recommendation citing unknown evidence: {rec.claim_ref} · {unknown}")
            continue
        source_count, agreeing, agents = _evidence_stats(rec.evidence_ids, signals)
        rec.confidence = confidence_fn(source_count, agreeing, agents)
        kept.append(rec)
    return kept[:MAX_RECOMMENDATIONS]


def _rebuild_opportunities(
    opportunities: list[Opportunity],
    valid_evidence_ids: set[str],
    emit,
) -> list[Opportunity]:
    kept = []
    for opp in opportunities:
        unknown = [eid for eid in opp.evidence_ids if eid not in valid_evidence_ids]
        if unknown:
            emit("strategist",
                 f"dropped opportunity citing unknown evidence: {opp.claim_ref} · {unknown}")
            continue
        kept.append(opp)
    return kept


def _evidence_stats(evidence_ids: list[str], signals: list[Signal]) -> tuple[int, int, int]:
    """Bridges a cited evidence_id list to the (source_count, agreeing,
    corroborating_agents) triple Gati's confidence_fn expects. Confirm this
    aggregation matches confidence.py's intent — merge.py owns the
    authoritative agreement definition (same type+competitor from 2+
    sources); this is a lightweight reconstruction from the signals this
    module already has in hand, not a second source of truth."""
    cited = set(evidence_ids)
    agents_seen: set[str] = set()
    type_competitor_pairs: set[tuple[str, str]] = set()

    for s in signals:
        if cited & set(s.evidence_ids):
            agents_seen.add(s.agent)
            type_competitor_pairs.add((s.type, s.competitor))

    source_count = len(cited)
    agreeing = len(type_competitor_pairs)
    corroborating_agents = len(agents_seen)
    return source_count, agreeing, corroborating_agents