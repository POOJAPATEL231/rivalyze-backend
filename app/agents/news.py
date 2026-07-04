"""News agent: for each competitor, search recent web results and extract
strategically relevant events — launches, funding, partnerships, leadership, and
pricing moves — as typed signals, each grounded to a real source URL.

The prompt asks the model to copy URLs verbatim from the corpus; a post-filter
then enforces it in code: an item survives only if its source_url is http(s) and
appears verbatim in the corpus the model was shown. A thin corpus, zero grounded
survivors, or lane exhaustion degrades a competitor to a typed low_signal result
plus an event. This function never raises; each competitor has its own guard, so
one rival failing never affects the others."""
from __future__ import annotations

import logging
import re
from datetime import datetime

from pydantic import BaseModel

from app.core import config
from app.core.llm_router import complete
from app.core.search_chain import search
from app.models import NewsItem, NewsSignals

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CORPUS_CAP = config.CORPUS_CAP    # 6500, or 12000 under RICH_SEARCH
_LOW_SIGNAL_THRESHOLD = 300
# Report keeps all grounded events; frontend shows the top few. Bound only as a
# DoS ceiling on model output (must stay <= NewsSignals.items max_length).
_MAX_ITEMS = 10


class _NewsExtraction(BaseModel):
    """Inner LLM schema: the model returns bare items; competitor and low_signal
    live on NewsSignals, which is assembled in code so the model cannot assert
    pipeline fields."""
    items: list[NewsItem]


def run(competitors: list[str], emit,
        *, search_fn=None, complete_fn=None) -> list[NewsSignals]:
    """Gather recent strategic news: one NewsSignals per competitor, in input
    order. Never raises. search_fn/complete_fn are keyword-only injection points
    for tests and default to the shared search and model-router functions."""
    search_fn = search_fn or search
    complete_fn = complete_fn or complete

    month = datetime.now().strftime("%B %Y")
    return [_scan_one(c, month, search_fn, complete_fn, emit) for c in competitors]


def _scan_one(competitor: str, month: str, search_fn, complete_fn, emit) -> NewsSignals:
    """One competitor's search -> extract -> ground cycle. Any failure (dead lane,
    malformed search row, unexpected error) degrades only this rival to a typed
    low_signal result; the sweep and every other competitor survive, and run()
    never raises."""
    low = NewsSignals(competitor=competitor, items=[], low_signal=True)
    emit("news", f"scanning recent events: {competitor}")
    try:
        corpus = _gather_corpus(competitor, month, search_fn, emit)
        if len(corpus) < _LOW_SIGNAL_THRESHOLD:
            emit("news", f"low signal: {competitor} · corpus too thin, skipping extraction")
            return low

        extraction, lane = complete_fn("extract", _prompt(competitor, corpus),
                                       _NewsExtraction, emit)
        items = _post_filter(extraction.items, corpus)
        if not items:
            emit("news", f"low signal: {competitor} · 0 items survived grounding")
            return low
        emit("news", f"{competitor}: {len(items)} grounded items via {lane}")
        return NewsSignals(competitor=competitor, items=items)
    except Exception as e:
        logger.warning("news: failed for %s: %s", competitor, e)
        emit("news", f"low signal: {competitor} · {type(e).__name__}: {e}")
        return low


def _gather_corpus(competitor: str, month: str, search_fn, emit) -> str:
    """Two recency-biased queries into one citation-bearing corpus, capped at
    _CORPUS_CAP characters. Each search row is read defensively; a row with no URL
    still contributes its text as context but gets no SOURCE line, so nothing
    derived from it can clear the grounding filter."""
    corpus = ""
    for q in (f"{competitor} latest news {month}",
              f"{competitor} product launch funding {month}",
              f"{competitor} partnership expansion strategy {month}"):
        for r in search_fn(q, emit):
            title = str(r.get("title", "")).strip()
            content = str(r.get("content", "")).strip()
            url = str(r.get("url", "")).strip()
            corpus += f"{title}\n{content}\n"
            if url:
                corpus += f"SOURCE: {url}\n"
            corpus += "\n"
    return corpus[:_CORPUS_CAP]


def _prompt(competitor: str, corpus: str) -> str:
    return f"""Extract strategically relevant recent events for {competitor} from the corpus below.
Include ONLY: launches, funding, partnerships, leadership, pricing moves.

=== CORPUS START ===
{corpus}
=== CORPUS END ===

Rules (use ONLY text between CORPUS START and CORPUS END):
- Every item MUST carry the exact source URL COPIED character-for-character from a
  SOURCE: line in the corpus. It MUST begin with http and MUST appear verbatim above.
  If you cannot find a real SOURCE URL for an event, DROP the item entirely.
- impact = one line on the strategic threat or opportunity this creates for
  companies competing with {competitor}.
- date = YYYY-MM-DD if the corpus states it, else "".
- Return up to 10 items — capture EVERY distinct strategically relevant event the
  corpus supports (the report keeps them all). Never pad or invent: if the corpus
  supports only 2 real events, return 2. No supported events -> {{"items": []}}.

WRONG (never do this):
{{"source_url": "News article"}}
{{"source_url": "TechCrunch"}}
{{"source_url": "https://example.com/made-up-link"}}
RIGHT:
{{"source_url": "https://techcrunch.com/2026/06/12/acme-raises-series-b"}} (copied exactly from a SOURCE: line)

The top-level object has exactly one key: "items". Every field value is a plain JSON
string — no nested objects, no null. Return ONLY the JSON object, no markdown, no prose:
{{"items":[{{"event":"","impact":"","source_url":"https://...","date":"YYYY-MM-DD or ''"}}]}}"""


def _post_filter(items: list[NewsItem], corpus: str) -> list[NewsItem]:
    """The grounding filter: honesty enforced in code, not requested in prose. Keep
    an item only if its source_url is http(s) and a verbatim member of the corpus
    SOURCE lines; collapse duplicate events case- and whitespace-insensitively;
    blank non-ISO dates instead of dropping the item; cap at 4 after validation so
    invalid items never consume cap slots."""
    corpus_urls = {line.removeprefix("SOURCE: ").strip()
                   for line in corpus.splitlines() if line.startswith("SOURCE: ")}
    kept: list[NewsItem] = []
    seen: set[str] = set()
    for item in items:
        url = item.source_url.strip()
        if not url.startswith(("http://", "https://")) or url not in corpus_urls:
            continue
        key = " ".join(item.event.lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        date = item.date.strip()
        kept.append(item.model_copy(update={
            "source_url": url,
            "date": date if _DATE_RE.fullmatch(date) else "",
        }))
    return kept[:_MAX_ITEMS]
