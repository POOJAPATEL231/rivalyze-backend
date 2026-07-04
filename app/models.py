"""Rivalyze shared Pydantic models — THE backend contract.

Owner: Drashti (changes = contract changes, announce them). Every agent and
graph node imports from here; node boundaries validate against these models so
silent garbage crashes at the seam instead of leaking into the report.

Two layers live here:
  1. Domain models (discovery → news/product/review → merge → strategist report)
     — the typed payloads agents produce and the graph carries.
  2. API/run-lifecycle models — the request/response and poll shapes the frozen
     /api/v1 contract returns.
"""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


# ============================ domain: discovery ============================
class Competitor(BaseModel):
    # Bounded because these come from LLM output (potentially prompt-steered) and
    # are echoed back to the user / persisted; caps limit the blast radius.
    name: str = Field(max_length=120)
    category: Literal["direct", "indirect"] = "direct"
    rationale: str = Field(default="", max_length=400)


class CompetitorSet(BaseModel):
    """Discovery output. Max 4 rivals, never includes the input company."""

    competitors: list[Competitor] = Field(default_factory=list, max_length=4)


# ============================== domain: news ==============================
class NewsItem(BaseModel):
    event: str
    impact: str
    source_url: str  # must be a real URL from corpus; validated in the agent
    date: str = ""


class NewsSignals(BaseModel):
    competitor: str
    items: list[NewsItem] = Field(default_factory=list, max_length=4)
    low_signal: bool = False


# ============================ domain: product =============================
class ProductIntel(BaseModel):
    competitor: str
    pricing_tiers: list[str] = Field(default_factory=list)  # PLAIN strings, never objects
    recent_features: list[str] = Field(default_factory=list)
    positioning: str = ""
    advantages: list[str] = Field(default_factory=list)  # framed FOR our company
    sources: list[str] = Field(default_factory=list)
    low_signal: bool = False


# ============================ domain: reviews =============================
class SentimentIntel(BaseModel):
    competitor: str
    top_complaints: list[str] = Field(default_factory=list, max_length=3)
    opportunity_gaps: list[str] = Field(default_factory=list, max_length=3)
    overall_sentiment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"] = "NEUTRAL"
    sources: list[str] = Field(default_factory=list)
    low_signal: bool = False


# ======================= domain: evidence & signals =======================
class EvidenceRow(BaseModel):
    id: str  # "ev-" + uuid4().hex[:8]
    run_id: str
    claim_ref: str  # "pricing:coda" / "rec:bundle-ai"
    source_type: Literal["news", "pricing", "review", "web", "document"]
    source_name: str
    url: str
    snippet: str = Field(max_length=280)
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
    per_competitor: dict = Field(default_factory=dict)  # rollups incl. evidence_ids
    low_signal_findings: list[str] = Field(default_factory=list)


# ============================ domain: report =============================
class Opportunity(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ref: str


class Recommendation(BaseModel):
    action: str
    rationale: str
    confidence: float = Field(ge=0.05, le=0.95)  # ALWAYS code-computed
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ref: str


class Swot(BaseModel):
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    threats: list[str] = Field(default_factory=list)


class SentimentScore(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    label: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"]


class H2HCell(BaseModel):
    value: str
    claim_ref: Optional[str] = None
    source_date: Optional[str] = None


class H2HRow(BaseModel):
    metric: str
    you: str
    rivals: dict[str, H2HCell] = Field(default_factory=dict)


class CompetitiveReport(BaseModel):
    company: str
    threat_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    executive_summary: str
    swot: Swot
    sentiment: dict[str, SentimentScore] = Field(default_factory=dict)
    head_to_head: list[H2HRow] = Field(default_factory=list)
    opportunities: list[Opportunity] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=3)
    low_signal_findings: list[str] = Field(default_factory=list)
    analysis_date: str


# ========================= API / run lifecycle ==========================
class AnalyzeRequest(BaseModel):
    # Untrusted user input. Length caps bound prompt/query cost and DoS surface;
    # the validator strips control chars so nothing corrupts the slug, event
    # ledger, logs, or (future) a Location header / DB column.
    company: str = Field(default="", max_length=200)
    domain: str = Field(default="", max_length=200)
    idea: Optional[str] = Field(default=None, max_length=500)  # idea mode: a pre-step infers company + domain

    @field_validator("company", "domain", "idea")
    @classmethod
    def _strip_control_chars(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return "".join(ch for ch in v if ch.isprintable()).strip()


class AnalyzeResponse(BaseModel):
    job_id: str
    status: str


class RunEvent(BaseModel):
    t: float    # seconds since run start
    agent: str  # discovery | router | search | merge | strategist | system
    msg: str


class RunStatus(BaseModel):
    """Poll shape for GET /api/v1/runs/{job_id}, polled every 2s by the UI.

    In this vertical slice `result` holds the discovery CompetitorSet. In the
    full pipeline the completed run persists a CompetitiveReport fetched via
    GET /api/v1/reports/{run_id}; `run_id` is populated on completion so the
    frontend can navigate to /dash/{run_id}.
    """

    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    current_stage: str = "queued"
    events: list[RunEvent] = Field(default_factory=list)
    result: Optional[CompetitorSet] = None
    lane_stats: dict[str, int] = Field(default_factory=dict)
    run_id: Optional[str] = None
    error: Optional[str] = None


class HistoryEntry(BaseModel):
    """One row of GET /api/v1/history. threat_level/confidence are optional:
    a completed run persisted before the strategist agent existed (or any
    run finished via finish_run(job_id) with no report yet) has neither."""

    job_id: str
    company: str
    threat_level: Optional[str] = None
    confidence: Optional[float] = None
    created_at: datetime


# ============================== auth (users) ==============================
def _within_bcrypt_limit(password: str) -> str:
    # bcrypt only considers the first 72 BYTES; reject longer so no silent
    # truncation surprises a user (a multibyte password can exceed 72 bytes
    # well under 72 characters).
    if len(password.encode("utf-8")) > 72:
        raise ValueError("password must be at most 72 bytes")
    return password


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, v: str) -> str:
        return _within_bcrypt_limit(v)


class LoginRequest(BaseModel):
    # No min_length here on purpose: the login form must not leak the password
    # policy. Length is only capped so bcrypt never sees oversized input.
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)

    @field_validator("password")
    @classmethod
    def _password_bytes(cls, v: str) -> str:
        return _within_bcrypt_limit(v)


class TokenResponse(BaseModel):
    access_token: str          # short-lived JWT (stateless)
    refresh_token: str         # long-lived opaque token (revocable, stored hashed)
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class UserPublic(BaseModel):
    user_id: str
    email: EmailStr
