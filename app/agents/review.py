"""Reviews agent — mines customer complaints and sentiment per competitor.

Owner: Mihir
Spec: docs/llm_prompts.md §REVIEWS (Agent 2)
Output: list[SentimentIntel]   — one entry per competitor, always populated.

Contract (frozen Sat 11:00):
    def run(
        competitors: list[str],
        emit: Callable[[str, str], None],
        company: str = "our company",
    ) -> list[SentimentIntel]

Guarantees:
    - top_complaints: ≤3 SHORT plain strings — NEVER nested dicts/objects
    - overall_sentiment: exactly one of POSITIVE | NEUTRAL | NEGATIVE
    - low_signal=True on corpus < 300 chars OR 0 search results
    - Never raises. The caller (orchestrator) ALWAYS gets a typed result per
      competitor, even if search and LLM both fail.

Anti-nesting guard:
    Weak models occasionally return {"issue": "...", "severity": "..."} for
    a complaint slot. `_flatten_complaint` collapses any accidental dict/list
    into a plain string so the Dashboard never renders "[object Object]".

Module contract: imports from app.core.{search_chain, llm_router, cache}
and app.models.SentimentIntel. The cache module is consumed only
opportunistically (corpus-key memoisation) — its absence is non-fatal.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Literal

from app.core.llm_router import complete
from app.core.search_chain import search
from app.models import SentimentIntel

logger = logging.getLogger(__name__)

# Refresh the month label each import so a long-running process still uses
# the current month in its search queries.
MONTH = datetime.now().strftime("%B %Y")  # e.g. "July 2026"

# Per-corpus low-signal threshold. Below this we skip the LLM call entirely
# — there is not enough text for an honest extraction.
LOW_SIGNAL_CORPUS_CHARS = 300

# Cap how much corpus we ship to the LLM. ~5k chars is enough for three
# complaints and keeps the prompt budget predictable.
CORPUS_CHAR_CAP = 5000

# Whitelist of acceptable sentiment values (mirror the Pydantic Literal).
_SENTIMENT_VALUES: tuple[Literal["POSITIVE", "NEUTRAL", "NEGATIVE"], ...] = (
    "POSITIVE",
    "NEUTRAL",
    "NEGATIVE",
)


# System prompt — kept verbatim from docs/llm_prompts.md §REVIEWS, with
# explicit anti-nesting examples added so weak models don't return objects.
_SYSTEM_PROMPT = """Mine customer complaints about {competitor} from the corpus
(reviews, Reddit, forums, app stores).

top_complaints: UP TO 3 SHORT plain strings ONLY. Example: "feature overload".
WRONG (do not do this): {{"issue": "overload", "severity": "high"}}
WRONG (do not do this): ["overload", "high"]
RIGHT: "feature overload"

opportunity_gaps: one exploitable gap per complaint, phrased as an
opportunity for {company} (e.g. "ship a lightweight tier without AI").

overall_sentiment: EXACTLY one of POSITIVE | NEUTRAL | NEGATIVE — no
other values. Default NEUTRAL if corpus is mixed.

sources: ONLY URLs that actually appear in the corpus above. Never invent.

Return ONLY JSON (no markdown fences, no commentary):
{{"top_complaints":[],"opportunity_gaps":[],"overall_sentiment":"NEUTRAL","sources":[]}}"""


# ---------- public API ----------

def run(
    competitors: list[str],
    emit: Callable[[str, str], None],
    company: str = "our company",
) -> list[SentimentIntel]:
    """Mine complaints and sentiment for each competitor.

    Args:
        competitors: list of rival company names (≤4 expected; cap not enforced here).
        emit: event emitter — same signature as every other agent. Used for
            run-monitor breadcrumbs. Never raises from inside `emit`.
        company: OUR company name; used to frame opportunity_gaps.

    Returns:
        One SentimentIntel per competitor, in input order. Never raises.
    """
    results: list[SentimentIntel] = []

    for competitor in competitors:
        try:
            result = _run_single(competitor, company, emit)
        except Exception as exc:  # last-resort safety net — caller always gets a row
            logger.error("reviews · unhandled error for %s: %s", competitor, exc)
            _safe_emit(emit, "reviews", f"reviews · error for {competitor}: {exc}")
            result = SentimentIntel(competitor=competitor, low_signal=True)
        results.append(result)

    return results


# ---------- internals ----------

def _run_single(
    competitor: str,
    company: str,
    emit: Callable[[str, str], None],
) -> SentimentIntel:
    """Process a single competitor. May raise — caller wraps in try/except."""

    # 1. Search — two query angles to broaden recall.
    raw_results: list[dict] = []
    for query in (
        f"{competitor} customer complaints problems {MONTH}",
        f"{competitor} negative reviews reddit {MONTH}",
    ):
        try:
            hits = search(query, emit)
        except Exception as exc:
            _safe_emit(emit, "reviews", f"reviews · search fail ({competitor}): {exc}")
            hits = []
        if hits:
            raw_results.extend(hits)

    # 2. Build the corpus — bound both per-result and total length.
    corpus = _build_corpus(raw_results)

    # 3. Low-signal guard — too little text means any extraction would be a guess.
    if not raw_results or len(corpus) < LOW_SIGNAL_CORPUS_CHARS:
        _safe_emit(emit, "reviews", f"reviews · low signal: {competitor}")
        return SentimentIntel(competitor=competitor, low_signal=True)

    # 4. LLM extraction via the hardened router.
    prompt = (
        _SYSTEM_PROMPT.format(competitor=competitor, company=company)
        + f"\n\nCORPUS:\n{corpus}"
    )
    try:
        model_instance, _lane = complete("extract", prompt, SentimentIntel, emit)
    except Exception as exc:
        # All lanes exhausted, JSON-repair failed, schema mismatch, etc.
        # The plan says callers convert this to low_signal.
        _safe_emit(emit, "reviews", f"reviews · llm fail ({competitor}): {exc}")
        return SentimentIntel(competitor=competitor, low_signal=True)

    # 5. Sanitise the model output — these guards are the difference between
    #    a Dashboard that renders cleanly and one that shows "[object Object]".
    return _sanitise(model_instance, competitor)


def _build_corpus(raw_results: list[dict]) -> str:
    """Concatenate the search hits into a single bounded corpus string."""
    parts: list[str] = []
    for r in raw_results:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        title = r.get("title") or ""
        content = r.get("content") or ""
        if not (url or title or content):
            continue
        parts.append(f"SOURCE: {url}\n{title}\n{content}")
    return "\n\n".join(parts)[:CORPUS_CHAR_CAP]


def _sanitise(model: SentimentIntel, competitor: str) -> SentimentIntel:
    """Return a SentimentIntel whose fields obey the contract.

    The router already returned a validated model — this pass is for the
    cases where a model is technically valid (Pydantic accepted it) but
    semantically wrong (e.g. a complaint that came back as a nested dict
    because the field type was forced to str via the model's coerce step).
    """
    # Flatten complaints — strip accidental nesting from weak models.
    clean_complaints: list[str] = []
    for c in (model.top_complaints or [])[:3]:
        flat = _flatten_complaint(c)
        if flat:
            clean_complaints.append(flat)

    clean_gaps: list[str] = []
    for g in (model.opportunity_gaps or [])[:3]:
        flat = _flatten_complaint(g)
        if flat:
            clean_gaps.append(flat)

    # Coerce sentiment to the enum. Anything else → NEUTRAL (safe default).
    sentiment = model.overall_sentiment if model.overall_sentiment in _SENTIMENT_VALUES else "NEUTRAL"

    # Sources: keep only http(s) URLs that look real. Cap to avoid prompt bloat.
    sources = [s for s in (model.sources or []) if isinstance(s, str) and s.startswith(("http://", "https://"))]
    if len(sources) > 8:
        sources = sources[:8]

    return SentimentIntel(
        competitor=competitor,
        top_complaints=clean_complaints,
        opportunity_gaps=clean_gaps,
        overall_sentiment=sentiment,  # type: ignore[arg-type]
        sources=sources,
        low_signal=model.low_signal,
    )


def _flatten_complaint(complaint) -> str:
    """Coerce any complaint-shaped value into a short plain string.

    Handles the weak-model failure modes observed in testing:
        - dict:  take the first string value found; else str(dict)
        - list:  take the first string element; else str(list)
        - other: str(value)
    Empty strings are returned as "" so the caller can filter them out.
    """
    if isinstance(complaint, str):
        return complaint.strip()
    if isinstance(complaint, dict):
        for v in complaint.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
            # Recurse into nested dict/list to mine a string leaf.
            if isinstance(v, (dict, list, tuple)):
                flat = _flatten_complaint(v)
                if flat and flat != str(v):
                    return flat
        return str(complaint)
    if isinstance(complaint, (list, tuple)):
        for v in complaint:
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, (dict, list, tuple)):
                flat = _flatten_complaint(v)
                if flat and flat != str(v):
                    return flat
        return str(complaint)
    return str(complaint)


def _safe_emit(emit: Callable[[str, str], None] | None, agent: str, msg: str) -> None:
    """Emit an event without ever raising — the agent must keep going.

    Args:
        emit: Emitter function that takes (agent: str, msg: str)
        agent: Identifier for the emitting component (e.g., "reviews")
        msg: Message to emit
    """
    if emit is None:
        return
    try:
        emit(agent, msg)
    except Exception:  # noqa: BLE001 — last-line defence
        logger.debug("emit sink rejected event %r", (agent, msg))
