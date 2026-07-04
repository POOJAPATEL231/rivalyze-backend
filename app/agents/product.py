"""
app/agents/product.py
Product intelligence agent — pricing tiers, features, positioning per competitor.
Cloned from: app/agents/discovery.py (same search -> LLM -> typed-output pattern),
adapted to the absolute-import + run(competitors, emit, company) shape from
tushar_plan.md rather than discovery.py's relative imports / (company, domain)
signature, since Gati's merge node and the spec assume this exact shape.

Output: list[ProductIntel] as dicts — one per competitor, always, never raises.
Owner: Tushar
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.core import config
from app.core.llm_router import complete
from app.core.search_chain import search
from app.core.grounding import ground_sources
from app.models import ProductIntel

logger = logging.getLogger(__name__)

# Computed once at import time (not per-call) — "July 2026" stays correct for
# the whole hackathon; see plan doc gotcha #7.
_MONTH = datetime.now().strftime("%B %Y")
_CORPUS_CAP = config.CORPUS_CAP    # 6500, or 12000 under RICH_SEARCH
_LOW_SIGNAL_THRESHOLD = 300

# Bare-JSON system prompt. The "PLAIN STRINGS" + wrong-example line is the
# single most important line in this file — LLMs default to nesting pricing
# into {"tier":..., "price":...} objects, which breaks Krutarth's h2h table.
#
# NOTE (Tushar): this template's example JSON used to omit "competitor"
# entirely, even though ProductIntel requires it. That was invisible under
# MOCK_MODE — the mock lane always injects a "competitor": "mock" placeholder
# regardless of what the prompt asks for — but a real Gemini call follows the
# example JSON literally and leaves the key out, which fails Pydantic
# validation with "Field required" and every extraction degrades to
# low_signal. `_process()` re-stamps `.competitor` with the caller's
# known-good name after validation anyway (see below), so the value the
# model puts here doesn't need to be accurate, just present.
_SYSTEM = (
    'Extract pricing and product positioning for {competitor}. '
    'pricing_tiers are PLAIN STRINGS like "Pro $12/seat: AI included" — '
    'NEVER nested objects (wrong example: {{"tier":"Pro","price":12}}). '
    'advantages = angles {company} can use AGAINST them, from the corpus only. '
    'Every source in "sources" must be a real URL from the corpus — never invent URLs. '
    '"competitor" must be exactly "{competitor}". '
    'ONLY JSON: {{"competitor":"{competitor}","pricing_tiers":[],"recent_features":[],"positioning":"","advantages":[],"sources":[]}}'
)


def run(competitors: list, emit, company: str = "") -> list[dict]:
    """
    Extract product intel for each competitor. Returns list[ProductIntel] as
    dicts (one per input competitor, always — never raises, never drops an
    entry). `competitors` items may be plain name strings or dicts with a
    "name" key (LangGraph orchestrator format).
    """
    results = []
    for item in competitors:
        name = item if isinstance(item, str) else item.get("name", str(item))
        try:
            intel = _process(name, company, emit)
        except Exception as e:
            # Per-competitor guard: a search-chain crash or any unexpected error
            # for ONE rival degrades only that rival to low_signal — it never
            # raises out of run() and never drops the rest of the batch.
            logger.warning("product: unhandled error for %s: %s", name, e)
            emit("product", f"low signal: {name} · {type(e).__name__}")
            intel = _low_signal(name)
        results.append(intel.model_dump())
    return results


def _process(competitor: str, company: str, emit) -> ProductIntel:
    emit("product", f"processing {competitor}")
    corpus = _build_corpus(competitor, company, emit)

    if len(corpus) < _LOW_SIGNAL_THRESHOLD:
        emit("product", f"low signal: thin corpus for {competitor} ({len(corpus)} chars)")
        return _low_signal(competitor)

    prompt = _SYSTEM.format(competitor=competitor, company=company or "our company")
    prompt += f"\n\nCORPUS:\n{corpus}"

    try:
        result, lane = complete("extract", prompt, ProductIntel, emit)
        # Stamp the known-good name over whatever the LLM returned — models
        # drift on capitalization ("click up" vs "ClickUp"); the caller's
        # input string is always authoritative here, not the model's output.
        result.competitor = competitor
        # Drop any source URL the model invented — keep only those that appear
        # verbatim in the corpus, so every evidence row is a real, retrievable
        # source (parity with the news agent's grounding).
        result.sources = ground_sources(result.sources, corpus)
        emit("product", f"{competitor} · {len(result.pricing_tiers)} tiers via {lane}")
        return result
    except RuntimeError as e:
        # complete() raises RuntimeError only when every LLM lane is
        # exhausted (per llm_router.py docstring) — that's the one failure
        # mode we expect here, so it's caught specifically rather than a
        # bare `except Exception` that could also swallow real bugs.
        emit("product", f"low signal: extraction failed for {competitor}: {e}")
        logger.warning("product: failed for %s: %s", competitor, e)
        return _low_signal(competitor)


def _build_corpus(competitor: str, company: str, emit) -> str:
    queries = [f"{competitor} pricing plans {_MONTH}"]
    if company:
        # Comparison articles tend to be the richest source for advantages —
        # only fired when we actually know our own company name.
        queries.append(f"{competitor} vs {company} comparison {_MONTH}")
    queries.append(f"{competitor} new features product update 2026")
    # Positioning/target-segment angle — feeds the "Market Position" and
    # "Target Segment" head-to-head rows the strategist now asks for.
    queries.append(f"{competitor} target market customers positioning")

    seen_urls: set[str] = set()
    parts: list[str] = []
    for q in queries:
        for item in search(q, emit):
            url = item.get("url", "")
            if url and url in seen_urls:
                continue
            seen_urls.add(url)
            parts.append(f"{item.get('title', '')}\n{item.get('content', '')}\nSOURCE: {url}\n")

    return "\n".join(parts)[:_CORPUS_CAP]


def _low_signal(competitor: str) -> ProductIntel:
    return ProductIntel(competitor=competitor, pricing_tiers=[], recent_features=[],
                        positioning="", advantages=[], sources=[], low_signal=True)
