"""Idea pre-step: the one call that turns idea-mode into company-mode.

When the request carries an idea but no company, this converts the free-text idea
into a coined product name and a short, searchable market description, then the
rest of the pipeline runs on (company, domain) as if a human had typed them. It is
a private extraction with its own schema, not part of the shared contract.

This never raises. Garbage input (empty strings, long rambles, non-English, an
idea that names a real company, pure emoji) still yields a sane, searchable result
via a heuristic fallback — there is no boundary in front of this call, so its own
fallback is the only net."""
from __future__ import annotations

import re
from typing import Callable, Optional

from pydantic import BaseModel, field_validator

from app.core.llm_router import complete

EmitFn = Callable[[str, str], None]
CompleteFn = Callable[..., tuple[BaseModel, str]]

_MAX_DOMAIN_WORDS = 8
_FALLBACK_WORDS = 6

_DEFAULT_COMPANY = "your venture"
_FALLBACK_DOMAIN = "early-stage product concept"

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "to", "for", "of", "in",
    "on", "at", "by", "with", "and", "or", "but", "that", "this", "it",
    "we", "our", "i", "want", "would", "like", "build", "building", "app",
    "platform", "startup", "idea", "basically", "just", "really", "very",
}


class IdeaDomain(BaseModel):
    """Extraction schema. company defaults to a displayable placeholder rather than
    an empty string, which the UI and discovery both treat as a valid name."""
    company: str = _DEFAULT_COMPANY
    domain: str

    @field_validator("company")
    @classmethod
    def _non_blank_company(cls, v: str) -> str:
        return v.strip() or _DEFAULT_COMPANY


def idea_to_domain(idea: str, emit: EmitFn, *, complete_fn: Optional[CompleteFn] = None) -> IdeaDomain:
    """One extraction call, then a code-side safety net. Never raises. complete_fn
    is a keyword-only injection point for tests and defaults to the shared
    model-router."""
    idea = (idea or "").strip()
    if not idea:
        emit("system", "idea pre-step: empty idea · heuristic fallback")
        return IdeaDomain(company=_DEFAULT_COMPANY, domain=_FALLBACK_DOMAIN)

    complete_fn = complete_fn or complete

    emit("system", "idea pre-step: converting idea to a searchable market definition")
    try:
        result, lane = complete_fn("extract", _prompt(idea), IdeaDomain, emit)
        domain = _clamp_domain(result.domain)
        if len(domain.split()) < 1:
            raise ValueError("empty domain from extraction")
        company = _strip_wrapping(result.company) or _DEFAULT_COMPANY
        resolved = IdeaDomain(company=company, domain=domain)
        emit("system", f'idea pre-step: via {lane} · company="{resolved.company}" · domain="{resolved.domain}"')
        return resolved
    except Exception as exc:
        emit("system", f"idea pre-step: low signal ({type(exc).__name__}) · heuristic fallback")
        return _heuristic_fallback(idea)


def _prompt(idea: str) -> str:
    return f"""Convert a startup idea into a market definition a competitor search would use.

IDEA:
{idea}

Rules:
- "company" is a coined, plausible two-word product name for this idea, OR the
  exact string "your venture" if no reasonable name suggests itself. Never
  return an existing real company's name.
- "domain" is a 5-8 word market description (the kind of phrase you'd type
  into a search box to find this idea's competitors) — a product category and
  buyer, not a restatement of the idea's prose.
- If the idea is empty, incoherent, non-English, or otherwise ungradable,
  still return your best short guess — never refuse, never return an error
  object.

Return ONLY a JSON object, no markdown, no prose:
{{"company":"<coined two-word name or 'your venture'>","domain":"<5-8 word market description>"}}"""


def _strip_wrapping(text: str) -> str:
    """Strip surrounding quotes or a code fence a model may add despite the prompt."""
    text = re.sub(r"```(?:json)?", "", text).strip()
    return text.strip("\"' \n\t")


def _clamp_domain(domain: str) -> str:
    words = _strip_wrapping(domain).split()
    if len(words) > _MAX_DOMAIN_WORDS:
        words = words[:_MAX_DOMAIN_WORDS]
    return " ".join(words)


def _heuristic_fallback(idea: str) -> IdeaDomain:
    """No LLM result available: reduce the raw text to a short searchable phrase
    good enough for discovery's own queries, with a last-resort default."""
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]*", idea.lower())
    meaningful = [w for w in words if w not in _STOPWORDS] or words
    domain = " ".join(meaningful[:_FALLBACK_WORDS]) or _FALLBACK_DOMAIN
    return IdeaDomain(company=_DEFAULT_COMPANY, domain=domain)
