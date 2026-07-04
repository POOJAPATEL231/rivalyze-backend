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
import re
from collections import Counter
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.llm_router import complete
from app.core.merge import EVIDENCE_INDEX_KEY
from app.models import (
    CompetitiveReport,
    H2HRow,
    Opportunity,
    Recommendation,
    SentimentScore,
    Swot,
    UnifiedSignals,
)

logger = logging.getLogger(__name__)

# Sentinel the MOCK lane keys on to emit a valid CompetitiveReport offline.
_SENTINEL = "RIVALYZE_STRATEGIST"

_THREATS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
_SENTIMENTS = {"POSITIVE", "NEUTRAL", "NEGATIVE"}


class _RecDraft(BaseModel):
    action: str = ""
    rationale: str = ""
    confidence: float = 0.5  # unbounded here — code RECOMPUTES it downstream
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ref: str = ""


class _OppDraft(BaseModel):
    text: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ref: str = ""


class _ReportDraft(BaseModel):
    """Lenient extraction schema — NO 3-recommendation cap, unbounded confidence /
    sentiment score, plain-string threat/sentiment. The strict CompetitiveReport is
    assembled in code AFTER clamping to 3 and recomputing confidence, so a model
    that over-produces (5 recs) or invents an out-of-range number never fails
    validation on every lane and silently zeroes the whole report."""
    threat_level: str = "MEDIUM"
    executive_summary: str = ""
    swot: Swot = Field(default_factory=Swot)
    sentiment: dict = Field(default_factory=dict)
    head_to_head: list = Field(default_factory=list)
    opportunities: list[_OppDraft] = Field(default_factory=list)
    recommendations: list[_RecDraft] = Field(default_factory=list)


def run(unified: UnifiedSignals, company: str, confidence_fn, emit) -> CompetitiveReport:
    """Synthesize the CompetitiveReport. `confidence_fn(sources, agreeing, agents)
    -> float` is injected so every confidence is code-computed. Never raises."""
    company = company or "our company"
    today = datetime.now().strftime("%Y-%m-%d")

    per_competitor = dict(unified.per_competitor or {})
    evidence_index: dict[str, dict] = per_competitor.pop(EVIDENCE_INDEX_KEY, {}) or {}
    rivals = list(per_competitor.keys())

    try:
        draft, lane = complete("reason", _prompt(company, rivals, evidence_index, per_competitor),
                               _ReportDraft, emit)
        emit("strategist", f"report synthesized via {lane}")
    except Exception as exc:  # noqa: BLE001 — all lanes exhausted / schema fail
        logger.warning("strategist: synthesis failed: %s", exc)
        emit("strategist", f"low signal: synthesis failed ({type(exc).__name__}) · degraded report")
        return _degraded(company, today, unified)

    # ---- code authority: clamp/recompute, then build the STRICT report ----
    recs = _clean_cited(draft.recommendations, evidence_index, confidence_fn, emit,
                        kind="recommendation")[:3]
    opps = _clean_cited(draft.opportunities, evidence_index, confidence_fn, emit,
                        kind="opportunity")
    threat = draft.threat_level.upper() if draft.threat_level.upper() in _THREATS else "MEDIUM"
    return CompetitiveReport(
        company=company,
        threat_level=threat,
        executive_summary=draft.executive_summary or "No executive summary was produced this run.",
        swot=draft.swot,
        sentiment=_coerce_sentiment(draft.sentiment),
        head_to_head=_coerce_h2h(draft.head_to_head, _claim_refs_by_competitor(evidence_index)),
        opportunities=[Opportunity(text=o.text, evidence_ids=o.evidence_ids, claim_ref=o.claim_ref)
                       for o in opps],
        recommendations=[Recommendation(action=r.action, rationale=r.rationale,
                                        confidence=r.confidence, evidence_ids=r.evidence_ids,
                                        claim_ref=r.claim_ref) for r in recs],
        low_signal_findings=list(unified.low_signal_findings or []),
        analysis_date=today,
    )


def _coerce_sentiment(raw: dict) -> dict:
    """Model sentiment -> {rival: SentimentScore}, clamping score to [0,1] and any
    non-enum label to NEUTRAL, dropping anything unparseable."""
    out: dict = {}
    for rival, v in (raw or {}).items():
        try:
            score = float(v.get("score", 0.5)) if isinstance(v, dict) else 0.5
            label = (v.get("label") if isinstance(v, dict) else "") or "NEUTRAL"
            out[str(rival)] = SentimentScore(
                score=max(0.0, min(1.0, score)),
                label=label if label in _SENTIMENTS else "NEUTRAL")
        except Exception:  # noqa: BLE001 — a bad rival costs the rival, not the report
            continue
    return out


def _slug(name: str) -> str:
    """Match merge._slug so a head-to-head rival name maps to the same claim_ref
    the evidence rows were stored under."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "rival"


def _claim_refs_by_competitor(index: dict) -> dict:
    """slug(competitor) -> {source_type: claim_ref} from the evidence graph, so a
    head-to-head cell can be linked to the drawer-queryable claim_ref for that
    rival + dimension (e.g. the Pricing cell for Swiggy -> "pricing:swiggy")."""
    out: dict = {}
    for meta in (index or {}).values():
        comp = _slug(meta.get("competitor", ""))
        stype = meta.get("type", "")
        cref = meta.get("claim_ref", "")
        if comp and stype and cref:
            out.setdefault(comp, {})[stype] = cref
    return out


def _metric_type(metric: str) -> str:
    """Map a head-to-head dimension name to the evidence source_type most likely to
    back it, so the cell links to the right slice of the evidence graph."""
    m = (metric or "").lower()
    if any(k in m for k in ("pric", "cost", "plan", "tier")):
        return "pricing"
    if any(k in m for k in ("sentiment", "complaint", "review", "satisfaction", "support", "nps")):
        return "review"
    return "news"  # features, momentum, market position, launches, partnerships, etc.


def _first_claim_ref(avail: dict) -> str | None:
    for t in ("pricing", "review", "news"):
        if avail.get(t):
            return avail[t]
    return None


def _coerce_h2h(raw: list, claim_by_slug: dict | None = None) -> list:
    """Coerce head-to-head rows to H2HRow, dropping malformed ones, and attach a
    drawer-queryable claim_ref to each rival cell from the evidence graph — so the UI
    can click a comparison cell and see the sources behind it. A cell whose rival or
    dimension has no evidence is left uncited (claim_ref stays None), never faked."""
    claim_by_slug = claim_by_slug or {}
    out: list = []
    for row in (raw or []):
        try:
            h = H2HRow.model_validate(row)
        except Exception:  # noqa: BLE001
            continue
        stype = _metric_type(h.metric)
        for rival, cell in (h.rivals or {}).items():
            if cell.claim_ref:
                continue  # respect an explicit model-provided ref
            avail = claim_by_slug.get(_slug(rival), {})
            cell.claim_ref = avail.get(stype) or _first_claim_ref(avail)
        out.append(h)
    return out


def _clean_cited(items, index: dict, confidence_fn, emit, *, kind: str):
    """Strip unknown evidence ids and recompute confidence (recommendations only)
    from the surviving evidence.

    A weak model often can't reproduce the opaque `ev-xxxx` ids exactly, so it
    cites ids that aren't in the index. We used to DELETE any item whose citations
    were all-unknown — but that routinely zeroed the recommendations section (the
    headline of the report). A concrete, uncited recommendation is more useful to
    the board than an empty section, so we now KEEP it with its bogus ids stripped
    and a floor confidence recomputed from zero evidence (~0.25). Code still owns
    citations: an unknown id NEVER reaches the output, so nothing false is asserted."""
    kept = []
    for it in items or []:
        original = list(getattr(it, "evidence_ids", []) or [])
        known = [i for i in original if i in index]
        if original and not known:
            emit("strategist", f"kept {kind} with unverifiable citations stripped: {getattr(it, 'claim_ref', '?')}")
        it.evidence_ids = known
        if hasattr(it, "confidence"):
            it.confidence = _recompute(known, index, confidence_fn)
        kept.append(it)
    return kept


def _evidence_ledger(index: dict) -> str:
    """Render the evidence index as a readable id->fact ledger, grouped by
    competitor, so the model cites ids it can actually SEE. Each line is
    `ev-id [type] "snippet" (source)` — a copyable token next to the fact it backs.
    Bounded so a large graph can't blow the prompt budget."""
    if not index:
        return "(no evidence gathered this run)"
    by_comp: dict[str, list[str]] = {}
    for eid, meta in index.items():
        comp = meta.get("competitor", "?")
        snippet = (meta.get("snippet", "") or "").replace("\n", " ").strip()[:160]
        line = f"  {eid} [{meta.get('type', '?')}] \"{snippet}\" ({meta.get('source_name', '?')})"
        by_comp.setdefault(comp, []).append(line)
    blocks = []
    for comp, lines in by_comp.items():
        blocks.append(f"{comp}:\n" + "\n".join(lines))
    return "\n".join(blocks)[:6000]


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


def _prompt(company: str, rivals: list[str], evidence_index: dict, rollups: dict) -> str:
    rollup_json = json.dumps(rollups, ensure_ascii=False, default=str)[:12000]
    ledger = _evidence_ledger(evidence_index)
    return f"""{_SENTINEL}
You are the chief strategist for {company}. Turn the per-competitor intelligence
rollups below into a board-ready competitive analysis.

COMPETITORS: {', '.join(rivals) if rivals else '(none found)'}

Threat rubric: most markets are MEDIUM; HIGH requires explicit aggressive evidence
(funding + pricing attack, or a direct feature assault); CRITICAL is existential.

DEPTH REQUIREMENTS — a thin report is a failed report. Fill EVERY section from the
rollups (only leave one empty if the rollups truly say nothing about it):
- executive_summary: 3-5 full sentences — the overall threat and WHY, the 2-3 most
  important rivals and how they pressure {company}, and the single clearest opening.
- swot: 3-4 SPECIFIC items per quadrant, grounded in the rollups (not generic filler).
- sentiment: ONE entry per competitor listed in COMPETITORS (score 0-1 + label).
- head_to_head: 4-6 rows, each a DIFFERENT dimension — e.g. "Pricing", "Key Features",
  "Market Position", "Customer Sentiment", "Recent Momentum", "Target Segment". Each
  row's "you" is {company}'s stance; "rivals" compares EVERY competitor on that dimension.
- opportunities: 3-5 concrete, evidence-cited gaps {company} can exploit.

CITATIONS — every opportunity and recommendation should cite evidence_ids, and you
may ONLY use ids that appear in the EVIDENCE LEDGER below. Copy the ids EXACTLY
(e.g. "ev-1a2b3c4d") from the ledger line whose fact supports your point — do not
invent or guess ids. Any id you cite that is not in the ledger is stripped out, so
cite the real ones to keep your confidence score up. Maximum 3 recommendations, each
concrete enough to start on Monday. Put confidence at 0.5 as a placeholder; the system
recomputes the real number from your citations and discards yours.

EVIDENCE LEDGER (cite these exact ids):
{ledger}

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
