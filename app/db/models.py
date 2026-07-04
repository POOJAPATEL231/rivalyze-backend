"""Typed data models for Rivalyze agents and reports.

Owner: Dharvi (real) — this is a STUB for the review agent to import against.
Contract source: docs/project_understanding.md §5 (Data Models) and docs/schema.sql.
Pydantic v2; LangGraph reducers use these typed shapes.

When the real `models.py` lands, this file should be replaced verbatim — the
`SentimentIntel` field names and types below are FROZEN and consumed by:
- app/agents/review.py (this PR)
- app/api/routes.py (report rendering)
- Frontend Dashboard (sentiment bars, complaint chips)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# --- Discovery ---

class Competitor(BaseModel):
    name: str
    category: Literal["direct", "indirect"] = "direct"
    rationale: str = ""


class CompetitorSet(BaseModel):
    competitors: list[Competitor] = Field(default_factory=list, max_length=4)


# --- News ---

class NewsItem(BaseModel):
    event: str
    impact: str
    source_url: str  # real URL from corpus, not a publication name
    date: str = ""


class NewsSignals(BaseModel):
    competitor: str
    items: list[NewsItem] = Field(default_factory=list, max_length=4)
    low_signal: bool = False


# --- Product ---

class ProductIntel(BaseModel):
    competitor: str
    pricing_tiers: list[str]   # PLAIN STRINGS e.g. "Pro $12/seat: AI included"
    recent_features: list[str]
    positioning: str = ""
    advantages: list[str]
    sources: list[str]
    low_signal: bool = False


# --- Reviews (Mihir owns this output type) ---
# Contract is FROZEN. Field names below are referenced by:
#   - app/agents/review.py  (writer)
#   - app/core/merge.py     (consumer of the per-competitor rollup)
#   - Frontend Dashboard    (sentiment bars, complaint chips)
# Changing a field name here is a breaking change to the Dashboard.

class SentimentIntel(BaseModel):
    competitor: str
    top_complaints: list[str] = Field(default_factory=list, max_length=3)
    # SHORT plain strings (e.g. "feature overload"). NEVER nested dicts/objects.
    opportunity_gaps: list[str] = Field(default_factory=list, max_length=3)
    # One exploitable gap per complaint, framed as an opportunity for our company.
    overall_sentiment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"] = "NEUTRAL"
    sources: list[str] = Field(default_factory=list)
    # Only real URLs that appear in the search corpus.
    low_signal: bool = False


# --- Evidence (Gati's merge output — stored in DB) ---

class EvidenceRow(BaseModel):
    id: str            # "ev-" + uuid4().hex[:8]
    run_id: str
    claim_ref: str     # e.g. "pricing:coda" or "rec:bundle-ai"
    source_type: Literal["news", "pricing", "review", "web", "document"]
    source_name: str
    url: str
    snippet: str       # ≤280 chars
    source_date: str = ""
    agent: str


class Signal(BaseModel):
    run_id: str
    agent: str
    competitor: str
    type: Literal["launch", "funding", "pricing", "feature", "complaint", "sentiment"]
    payload: dict
    evidence_ids: list[str] = Field(default_factory=list)


class UnifiedSignals(BaseModel):
    signals: list[Signal] = Field(default_factory=list)
    per_competitor: dict = Field(default_factory=dict)
    low_signal_findings: list[str] = Field(default_factory=list)


# --- Final Report ---

class Swot(BaseModel):
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    threats: list[str] = Field(default_factory=list)


class SentimentScore(BaseModel):
    label: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"] = "NEUTRAL"
    score: float = 0.5
    low_signal: bool = False


class H2HRow(BaseModel):
    competitor: str
    our_strengths: list[str] = Field(default_factory=list)
    their_strengths: list[str] = Field(default_factory=list)


class Recommendation(BaseModel):
    action: str
    rationale: str
    confidence: float = Field(ge=0.05, le=0.95)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ref: str


class Opportunity(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ref: str


class CompetitiveReport(BaseModel):
    company: str
    threat_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    executive_summary: str
    swot: Swot
    sentiment: dict[str, SentimentScore]
    head_to_head: list[H2HRow]
    opportunities: list[Opportunity]
    recommendations: list[Recommendation] = Field(max_length=3)
    low_signal_findings: list[str]
    analysis_date: str
