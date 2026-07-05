"""Evidence grounding — keep only source URLs that actually appear in the corpus.

The news agent has always enforced this inline (an item survives only if its
source_url is a verbatim member of the corpus SOURCE: lines). Product and review
asked the model for corpus-only URLs in the PROMPT but never enforced it in code,
so a plausible-looking invented URL could become an evidence row pointing at a
source that doesn't exist. This shared helper closes that gap: honesty enforced in
code, not merely requested in prose.
"""
from __future__ import annotations


def corpus_urls(corpus: str) -> set[str]:
    """Extract the set of source URLs the corpus actually contains — every line
    shaped `SOURCE: <url>` (the format all agent corpora use)."""
    return {ln.removeprefix("SOURCE: ").strip()
            for ln in corpus.splitlines() if ln.startswith("SOURCE: ")}


def ground_sources(sources: list[str], corpus: str) -> list[str]:
    """Return only the http(s) sources that appear verbatim on a SOURCE: line in
    the corpus, de-duplicated and order-preserving. A model-invented URL — even a
    well-formed https one — is dropped, so every evidence row is a real source."""
    allowed = corpus_urls(corpus)
    kept: list[str] = []
    seen: set[str] = set()
    for s in sources or []:
        u = (s or "").strip()
        if u.startswith(("http://", "https://")) and u in allowed and u not in seen:
            seen.add(u)
            kept.append(u)
    return kept
