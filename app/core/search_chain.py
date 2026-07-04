"""Search chain with the Commit-grade cache (lessons doc §3: search credits
are the binding constraint — every response is cached by normalized query).

POC cache is in-memory; Saturday's build swaps the dict for Redis + Postgres
behind the same two functions (cache_get/cache_set land in core/cache.py).
Tushar owns the hardened chain (Tavily -> Serper -> robots.txt scrape) — this
ported base keeps the same `search(query, emit)` signature and `stats` dict.
"""
import hashlib
import os

import httpx

MOCK = os.getenv("MOCK_MODE", "0") == "1"
_CACHE: dict[str, list[dict]] = {}
stats = {"searches": 0, "cache_hits": 0}


def _key(q: str) -> str:
    return hashlib.sha256(q.lower().strip().encode()).hexdigest()[:16]


def _mock_results(q: str) -> list[dict]:
    return [{"title": f"Result {i} for {q}", "url": f"https://example.com/{_key(q)}/{i}",
             "content": f"(mock) market article discussing {q} — rivals, pricing, launches."}
            for i in range(1, 4)]


def search(query: str, emit=lambda a, m: None) -> list[dict]:
    k = _key(query)
    if k in _CACHE:
        stats["cache_hits"] += 1
        emit("search", f'"{query}" · cache HIT')
        return _CACHE[k]
    stats["searches"] += 1
    if MOCK:
        emit("search", f'"{query}" · mock lane')
        results = _mock_results(query)
    else:
        results = _tavily(query, emit) or _ddgs(query, emit) or []
    _CACHE[k] = results
    return results


def _tavily(query: str, emit) -> list[dict] | None:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return None
    try:
        emit("search", f'"{query}" · tavily')
        r = httpx.post("https://api.tavily.com/search",
                       json={"api_key": key, "query": query, "max_results": 3},
                       timeout=25.0)
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "content": x.get("content", "")} for x in r.json().get("results", [])]
    except httpx.HTTPError:
        emit("search", "tavily failed · falling back")
        return None


def _ddgs(query: str, emit) -> list[dict] | None:
    try:
        from ddgs import DDGS  # optional fallback: pip install ddgs

        emit("search", f'"{query}" · ddgs fallback')
        return [{"title": r["title"], "url": r["href"], "content": r["body"]}
                for r in DDGS().text(query, max_results=3)]
    except Exception:
        emit("search", "ddgs unavailable")
        return None
