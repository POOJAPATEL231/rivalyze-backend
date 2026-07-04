"""Strategist — the reason-lane agent that turns the merged evidence graph into a
board-ready CompetitiveReport.

Owner: Sheel (paired with Virat). The LLM writes the narrative; CODE owns the
numbers and the citations. After the model returns, this module:
  - stamps company / analysis_date / low_signal_findings from system values;
  - RECOMPUTES every recommendation confidence via the injected confidence_fn from
    the CITED evidence — the model's asserted numbers are discarded;
  - DROPS any recommendation/opportunity whose citations are ALL unknown ids
    (a bogus citation deletes the item) and strips unknown ids from the rest;
  - clamps to ≤3 recommendations.

Never raises: total LLM failure degrades to a typed low-signal report (the
validate node downstream is the second guard). Adapted by the orchestrator via
strategist_node(run_fn, confidence_fn) which calls run(unified, company,
confidence_fn, emit).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime

from app.core.llm_router import complete
from app.core.merge import EVIDENCE_INDEX_KEY
from app.models import CompetitiveReport, Swot, UnifiedSignals

logger = logging.getLogger(__name__)

# Sentinel the MOCK lane keys on to emit a valid CompetitiveReport offline.
_SENTINEL = "RIVALYZE_STRATEGIST"


def run(unified: UnifiedSignals, company: str, confidence_fn, emit) -> CompetitiveReport:
    """Synthesize the CompetitiveReport. `confidence_fn(sources, agreeing, agents)
    -> float` is injected so every confidence is code-computed. Never raises."""
    company = company or "our company"
    today = datetime.now().strftime("%Y-%m-%d")

    per_competitor = dict(unified.per_competitor or {})
    evidence_index: dict[str, dict] = per_competitor.pop(EVIDENCE_INDEX_KEY, {}) or {}
    rivals = list(per_competitor.keys())
    valid_ids = sorted(evidence_index.keys())

    try:
        report, lane = complete("reason", _prompt(company, rivals, valid_ids, per_competitor),
                                 CompetitiveReport, emit)
        emit("strategist", f"report synthesized via {lane}")
    except Exception as exc:  # noqa: BLE001 — all lanes exhausted / schema fail
        logger.warning("strategist: synthesis failed: %s", exc)
        emit("strategist", f"low signal: synthesis failed ({type(exc).__name__}) · degraded report")
        return _degraded(company, today, unified)

    # ---- code authority: overwrite what the model must not own ----
    report.company = company
    report.analysis_date = today
    report.low_signal_findings = list(unified.low_signal_findings or [])

    report.recommendations = _clean_cited(
        report.recommendations, evidence_index, confidence_fn, emit, kind="recommendation")[:3]
    report.opportunities = _clean_cited(
        report.opportunities, evidence_index, confidence_fn, emit, kind="opportunity")
    return report


def _clean_cited(items, index: dict, confidence_fn, emit, *, kind: str):
    """Drop items whose citations are ALL unknown; strip unknown ids from the rest;
    recompute confidence (recommendations only) from the surviving evidence."""
    kept = []
    for it in items or []:
        original = list(getattr(it, "evidence_ids", []) or [])
        known = [i for i in original if i in index]
        if original and not known:
            emit("strategist", f"dropped {kind} citing only unknown evidence: {getattr(it, 'claim_ref', '?')}")
            continue
        it.evidence_ids = known
        if hasattr(it, "confidence"):
            it.confidence = _recompute(known, index, confidence_fn)
        kept.append(it)
    return kept


def _recompute(evidence_ids: list[str], index: dict, confidence_fn) -> float:
    """Confidence from the cited evidence graph: distinct sources, distinct agents,
    and the largest agreeing (competitor, type) cluster."""
    cited = [index[i] for i in evidence_ids if i in index]
    sources = len({c.get("url") for c in cited if c.get("url")}) or len(cited)
    agents = len({c.get("agent") for c in cited})
    groups = Counter((c.get("competitor"), c.get("type")) for c in cited)
    agreeing = max(groups.values()) if groups else 0
    return confidence_fn(sources, agreeing, agents)


def _degraded(company: str, today: str, unified: UnifiedSignals) -> CompetitiveReport:
    return CompetitiveReport(
        company=company,
        threat_level="MEDIUM",
        executive_summary=(
            f"Competitive analysis for {company} could not be fully synthesized from the "
            "available evidence this run. Re-run to gather more signal."),
        swot=Swot(),
        sentiment={},
        head_to_head=[],
        opportunities=[],
        recommendations=[],
        low_signal_findings=list(unified.low_signal_findings or [])
        + ["strategist: model unavailable — degraded report"],
        analysis_date=today,
    )


def _prompt(company: str, rivals: list[str], valid_ids: list[str], rollups: dict) -> str:
    rollup_json = json.dumps(rollups, ensure_ascii=False, default=str)[:8000]
    return f"""{_SENTINEL}
You are the chief strategist for {company}. Turn the per-competitor intelligence
rollups below into a board-ready competitive analysis.

COMPETITORS: {', '.join(rivals) if rivals else '(none found)'}
EVIDENCE_IDS: {', '.join(valid_ids) if valid_ids else '(none)'}

Threat rubric: most markets are MEDIUM; HIGH requires explicit aggressive evidence
(funding + pricing attack, or a direct feature assault); CRITICAL is existential.

Every opportunity and recommendation MUST cite evidence_ids drawn ONLY from the
EVIDENCE_IDS list above — citing an id not in that list deletes the item. Maximum 3
recommendations, each concrete enough to start on Monday. Put confidence at 0.5 as a
placeholder; the system recomputes the real number and discards yours.

ROLLUPS (per competitor, JSON):
{rollup_json}

Return ONLY a JSON object matching this CompetitiveReport schema (no prose, no fences):
{{"company":"{company}","threat_level":"LOW|MEDIUM|HIGH|CRITICAL","executive_summary":"",
 "swot":{{"strengths":[],"weaknesses":[],"opportunities":[],"threats":[]}},
 "sentiment":{{"<rival>":{{"score":0.0,"label":"POSITIVE|NEUTRAL|NEGATIVE"}}}},
 "head_to_head":[{{"metric":"","you":"","rivals":{{"<rival>":{{"value":""}}}}}}],
 "opportunities":[{{"text":"","evidence_ids":[],"claim_ref":""}}],
 "recommendations":[{{"action":"","rationale":"","confidence":0.5,"evidence_ids":[],"claim_ref":""}}],
 "low_signal_findings":[],"analysis_date":""}}"""
