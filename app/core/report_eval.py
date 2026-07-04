"""Report-quality evaluation — an LLM-as-judge that scores a finished report.

Adopted from rivalyze-dev (src/tools/report_evaluator.py), but wired through OUR
hardened llm_router (4-lane failover, multi-key, MOCK lane) instead of a bare
single-model call, and returning a validated schema instead of hand-parsed JSON.

Feature-flagged (config.REPORT_EVAL) and best-effort: it NEVER raises and NEVER
blocks a run — the caller treats a None result as "not scored this run".
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.core.llm_router import complete

logger = logging.getLogger(__name__)


class _Scores(BaseModel):
    """1-10 per dimension; lenient defaults so a partial model answer still validates."""
    completeness: float = Field(default=5.0)
    accuracy: float = Field(default=5.0)
    strategic_value: float = Field(default=5.0)
    actionability: float = Field(default=5.0)
    overall_score: float = Field(default=5.0)


def _clamp(x: float) -> float:
    try:
        return max(0.0, min(10.0, round(float(x), 1)))
    except (TypeError, ValueError):
        return 5.0


def _prompt(company: str, report: dict) -> str:
    import json
    body = json.dumps({k: report.get(k) for k in (
        "executive_summary", "swot", "sentiment", "head_to_head",
        "opportunities", "recommendations")}, ensure_ascii=False, default=str)[:6000]
    return f"""You are a STRICT evaluator of competitive-intelligence reports. Score the
report for {company} on each dimension from 1 (poor) to 10 (excellent):
- completeness: are all sections present and filled (swot, sentiment, head-to-head,
  opportunities, recommendations)?
- accuracy: are claims specific and evidence-backed, not generic filler?
- strategic_value: are the insights genuinely useful for business strategy?
- actionability: can the recommendations be started immediately?
- overall_score: your holistic 1-10 rating.

Return ONLY a JSON object: {{"completeness":8,"accuracy":7,"strategic_value":8,
"actionability":7,"overall_score":7.5}}

REPORT:
{body}"""


def evaluate(report: dict, company: str, emit=lambda a, m: None) -> dict | None:
    """Score a report. Returns {completeness,accuracy,strategic_value,actionability,
    overall_score} (each 0-10) or None if evaluation could not run. Never raises."""
    if not report:
        return None
    try:
        scores, lane = complete("reason", _prompt(company or "the company", report), _Scores, emit)
        out = {k: _clamp(v) for k, v in scores.model_dump().items()}
        emit("evaluator", f"report scored {out['overall_score']}/10 via {lane}")
        return out
    except Exception as exc:  # noqa: BLE001 — scoring is best-effort, never breaks a run
        logger.warning("report_eval: scoring failed: %s", exc)
        emit("evaluator", f"report scoring skipped ({type(exc).__name__})")
        return None
