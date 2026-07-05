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


def idea_to_domain(idea: str, emit: EmitFn, *, context: Optional[dict] = None,
                   complete_fn: Optional[CompleteFn] = None) -> IdeaDomain:
    """One extraction call, then a code-side safety net. Never raises. `context` is
    the optional structured founder intake (industry/geography/customer/model/stage,
    a dict or None): it informs the extraction prompt AND is folded deterministically
    into the resolved domain so discovery's geography-aware search reliably gets the
    target market even if the model omits it. complete_fn is a keyword-only injection
    point for tests and defaults to the shared model-router."""
    idea = (idea or "").strip()
    # "any context?" here works off the plain state dict the orchestrator passes, so it
    # reuses _context_lines rather than IdeaContext.is_empty() (the model-level check used
    # by the request layer). Same question, two layers — the dict is what reaches this call.
    has_ctx = bool(_context_lines(context))
    if not idea and not has_ctx:
        emit("system", "idea pre-step: empty idea · heuristic fallback")
        return IdeaDomain(company=_DEFAULT_COMPANY, domain=_FALLBACK_DOMAIN)

    complete_fn = complete_fn or complete

    emit("system", "idea pre-step: converting idea to a searchable market definition"
                   + (" (with founder context)" if has_ctx else ""))
    try:
        result, lane = complete_fn("extract", _prompt(idea, context), IdeaDomain, emit)
        domain = _clamp_domain(result.domain)
        if not domain.split() and not has_ctx:
            raise ValueError("empty domain from extraction")
        company = _strip_wrapping(result.company) or _DEFAULT_COMPANY
        domain = _apply_context(domain, context)   # enforce geography/industry deterministically
        if not domain.strip():
            raise ValueError("empty domain after context")
        resolved = IdeaDomain(company=company, domain=domain)
        emit("system", f'idea pre-step: via {lane} · company="{resolved.company}" · domain="{resolved.domain}"')
        return resolved
    except Exception as exc:
        emit("system", f"idea pre-step: low signal ({type(exc).__name__}) · heuristic fallback")
        return _heuristic_fallback(idea, context)


def _ctx_get(context: Optional[dict], key: str) -> str:
    if not context:
        return ""
    v = context.get(key, "") if isinstance(context, dict) else getattr(context, key, "")
    return (v or "").strip()


def _context_lines(context: Optional[dict]) -> str:
    """Render the provided (non-empty) context fields as prompt bullet lines."""
    fields = [("industry/space", "industry"), ("target geography", "target_geography"),
              ("target customer", "target_customer"), ("business model", "business_model"),
              ("stage", "stage")]
    return "\n".join(f"- {label}: {_ctx_get(context, key)}"
                     for label, key in fields if _ctx_get(context, key))


def _apply_context(domain: str, context: Optional[dict]) -> str:
    """Fold the highest-signal context into the market phrase deterministically, so
    discovery's geography-aware search gets it even if the model left it out: ensure
    the industry is present, then append the target geography (idempotent — never
    duplicates a term already in the phrase)."""
    domain = (domain or "").strip()
    industry = _ctx_get(context, "industry")
    if industry and industry.lower() not in domain.lower():
        domain = f"{industry} {domain}".strip() if domain else industry
    geo = _ctx_get(context, "target_geography")
    if geo and geo.lower() not in domain.lower():
        # "... in {geo}" when there's a phrase to qualify; just {geo} when the domain
        # is empty (only-geography, no idea/industry) — avoids a domain like "in India".
        domain = f"{domain} in {geo}".strip() if domain else geo
    return domain


def _prompt(idea: str, context: Optional[dict] = None) -> str:
    ctx = _context_lines(context)
    ctx_block = (f"\nFOUNDER-PROVIDED CONTEXT (authoritative — prefer this over guessing "
                 f"from the prose):\n{ctx}\n") if ctx else ""
    geo = _ctx_get(context, "target_geography")
    geo_rule = (f'\n- The market description MUST reflect "{geo}" so competitor search '
                f"surfaces players who actually operate in that market.") if geo else ""
    return f"""Convert a startup idea into a market definition a competitor search would use.

IDEA:
{idea or "(no free-text idea — build the market definition from the founder context below)"}
{ctx_block}
Rules:
- "company" is a coined, plausible two-word product name for this idea, OR the
  exact string "your venture" if no reasonable name suggests itself. Never
  return an existing real company's name.
- "domain" is a 5-8 word market description (the kind of phrase you'd type
  into a search box to find this idea's competitors) — a product category and
  buyer, not a restatement of the idea's prose.{geo_rule}
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


def _heuristic_fallback(idea: str, context: Optional[dict] = None) -> IdeaDomain:
    """No LLM result available: build a short searchable phrase from the founder's
    industry (best signal) or, failing that, the raw idea text — then fold in the
    rest of the context (geography). Last-resort default keeps discovery runnable."""
    industry = _ctx_get(context, "industry")
    if industry:
        base = industry
    else:
        words = re.findall(r"[a-zA-Z][a-zA-Z\-]*", (idea or "").lower())
        meaningful = [w for w in words if w not in _STOPWORDS] or words
        base = " ".join(meaningful[:_FALLBACK_WORDS])
    domain = _apply_context(base, context) or _FALLBACK_DOMAIN
    return IdeaDomain(company=_DEFAULT_COMPANY, domain=domain)
