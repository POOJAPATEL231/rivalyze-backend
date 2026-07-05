"""Search chain with the Commit-grade cache (lessons doc §3: search credits
are the binding constraint — every response is cached by normalized query).

POC cache is in-memory; Saturday's build swaps the dict for Redis + Postgres
behind the same two functions (cache_get/cache_set land in core/cache.py).
Tushar owns the hardened chain (Tavily -> Serper -> robots.txt scrape) — this
ported base keeps the same `search(query, emit)` signature and `stats` dict.

--- REWRITE NOTE (Tushar) ---
Replacing the previous ported-POC body below with the frozen chain from the
task spec: cache -> Tavily -> Serper -> direct scrape -> []. The old version
called `_ddgs()` (DuckDuckGo) as its fallback, which is explicitly banned by
spec ("NO ddgs. Do not use it, do not import it."), had no Serper step, no
domain-scrape step, and never called counter_incr(), so Sunday's credit
report would always read 0. All of that is fixed here. `MOCK` / `_mock_results`
behavior is preserved unchanged since other agents may rely on MOCK_MODE for
local dev without hitting real APIs.
"""
import hashlib
import ipaddress
import logging
import os
import socket
import urllib.robotparser
from urllib.parse import urlparse

import httpx

# Mihir's cache module (app/core/cache.py) does not exist in the repo yet as
# of this rewrite. Using the shim from the plan doc so this file works today
# and switches over automatically (no code change needed) the moment
# cache.py is merged, since the `try` import will then succeed instead of
# raising ImportError.
# QUESTION FOR MIHIR: once cache.py lands, please confirm cache_get/cache_set
# signatures match what's assumed here: cache_get(key) -> value|None,
# cache_set(key, value, ttl=86400) -> None. If the signature differs this
# shim (and the calls below) will need updating.
try:
    from app.core.cache import cache_get, cache_set
except ImportError:
    _LOCAL_CACHE: dict = {}

    def cache_get(key: str):
        return _LOCAL_CACHE.get(key)

    def cache_set(key: str, value, ttl: int = 86400):
        _LOCAL_CACHE[key] = value

from concurrent.futures import ThreadPoolExecutor

from app.core import config
from app.core.counters import counter_incr, today_key

logger = logging.getLogger(__name__)

MOCK = os.getenv("MOCK_MODE", "0") == "1"
stats = {"searches": 0, "cache_hits": 0}

_UA = "RivalyzeBot/0.1 (+hackathon demo)"
# Used to spot "is this query about a specific competitor's own domain"
# (step 5 of the spec). Kept as a plain suffix list rather than a regex for
# readability; extend this tuple if a competitor uses a TLD not covered here.
_DOMAIN_SUFFIXES = (".com", ".io", ".so", ".co", ".ai", ".app", ".net", ".org", ".dev", ".tech", ".ly")


def _key(q: str) -> str:
    return hashlib.sha256(q.lower().strip().encode()).hexdigest()[:16]


def _keys_for(key_env: str) -> list[str]:
    """All API keys configured for a provider, in rotation order. Mirrors
    app.core.llm_router._keys_for: ONE env var per provider, comma-separated
    for multiple keys (e.g. TAVILY_API_KEY=key1,key2) — one Key Vault secret
    name per provider."""
    keys: list[str] = []
    seen: set[str] = set()
    for k in os.getenv(key_env, "").split(","):
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def _mock_results(q: str) -> list[dict]:
    return [{"title": f"Result {i} for {q}", "url": f"https://example.com/{_key(q)}/{i}",
             "content": f"(mock) market article discussing {q} — rivals, pricing, launches."}
            for i in range(1, 4)]


def search(query: str, emit=lambda a, m: None) -> list[dict]:
    """FROZEN signature per spec: search(query, emit) -> list[dict]. Every
    agent (discovery/news/product/reviews) imports this exact signature —
    do not change the parameters.

    Order: cache -> Tavily -> Serper -> direct scrape (competitor domain
    queries only) -> []. Empty list is a valid, expected result — callers
    treat it as "low signal", not an error.
    """
    k = _key(query)

    cached = cache_get(k)
    if cached is not None:
        stats["cache_hits"] += 1
        emit("search", f'"{query}" · cache HIT')
        return cached

    stats["searches"] += 1

    if MOCK:
        emit("search", f'"{query}" · mock lane')
        results = _mock_results(query)
        if results:
            cache_set(k, results)
        return results

    results = _tavily(query, emit)
    if results:
        cache_set(k, results)
        return results

    results = _serper(query, emit)
    if results:
        cache_set(k, results)
        return results

    domain = _extract_domain(query)
    if domain:
        results = _scrape(f"https://{domain}", emit)
        if results:
            cache_set(k, results)
            return results

    emit("search", f'"{query}" · no results found')
    return []


def search_all(queries, emit=lambda a, m: None) -> list[dict]:
    """Run several searches CONCURRENTLY and return their combined results in query
    order. Each search() is independent network I/O, so this turns an agent's N
    sequential searches (sum of latencies) into ~one search's latency — the single
    biggest per-agent speedup. Same signature/behaviour as calling search() per query
    and concatenating, just in parallel. (The global stats counter may under-count by
    a hair under the race — a metrics-only cost, never a correctness one.)"""
    qs = [q for q in queries if q and q.strip()]
    if not qs:
        return []

    def _one(q):
        try:
            return search(q, emit)
        except Exception:  # noqa: BLE001 — one bad query must not sink the batch
            return []
    if len(qs) == 1:
        return _one(qs[0])
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(qs), 6)) as ex:
        for res in ex.map(_one, qs):   # ex.map preserves order
            out.extend(res)
    return out


def _tavily(query: str, emit) -> list[dict] | None:
    keys = _keys_for("TAVILY_API_KEY")
    if not keys:
        return None
    for ki, key in enumerate(keys, 1):
        tag = f" · key {ki}/{len(keys)}" if len(keys) > 1 else ""
        try:
            emit("search", f'"{query}" · tavily{tag}')
            r = httpx.post("https://api.tavily.com/search",
                           json={"api_key": key, "query": query,
                                 "max_results": config.SEARCH_MAX_RESULTS,
                                 "search_depth": config.SEARCH_DEPTH},
                           timeout=10.0)   # was 25s — a slow provider must fail over fast
            if r.status_code in (429, 401, 403) and ki < len(keys):
                emit("search", f"Tavily {r.status_code}{tag} exhausted · rotating key")
                continue
            r.raise_for_status()
            # Only count this as a spend against the daily budget once we know
            # the call actually succeeded — a 4xx/5xx below never reaches here.
            counter_incr(today_key("tavily"))
            return [{"title": x.get("title", ""), "url": x.get("url", ""),
                     "content": x.get("content", "")} for x in r.json().get("results", [])]
        except httpx.HTTPStatusError as e:
            emit("search", f"Tavily {e.response.status_code} · falling back to Serper")
            return None
        except httpx.HTTPError:
            emit("search", "tavily failed · falling back to Serper")
            return None
    emit("search", "tavily · all keys exhausted · falling back to Serper")
    return None


def _serper(query: str, emit) -> list[dict] | None:
    # CRITICAL per spec gotcha #1: Serper's response uses "link" (not
    # "url") and "snippet" (not "content") — mapped explicitly below so
    # callers always see the same {title, url, content} shape regardless
    # of which provider answered.
    keys = _keys_for("SERPER_API_KEY")
    if not keys:
        return None
    for ki, key in enumerate(keys, 1):
        tag = f" · key {ki}/{len(keys)}" if len(keys) > 1 else ""
        try:
            emit("search", f'"{query}" · serper{tag}')
            r = httpx.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"q": query, "num": config.SEARCH_MAX_RESULTS},
                timeout=8.0,   # was 20s
            )
            if r.status_code in (429, 401, 403) and ki < len(keys):
                emit("search", f"Serper {r.status_code}{tag} exhausted · rotating key")
                continue
            r.raise_for_status()
            counter_incr(today_key("serper"))
            return [{"title": x.get("title", ""), "url": x.get("link", ""),
                     "content": x.get("snippet", "")} for x in r.json().get("organic", [])]
        except httpx.HTTPStatusError as e:
            emit("search", f"Serper {e.response.status_code} · falling back to scraper")
            return None
        except httpx.HTTPError:
            emit("search", "serper failed · falling back to scraper")
            return None
    emit("search", "serper · all keys exhausted · falling back to scraper")
    return None


def _extract_domain(query: str) -> str | None:
    """Return the first domain-looking word in the query (ends with a known
    TLD suffix), or None. This is what gates step 5 — direct scraping only
    ever runs for queries that explicitly name a competitor's own domain
    (e.g. "notion.so pricing"), never as a generic fallback."""
    for word in query.split():
        w = word.strip("\"'.,;:()")
        for suffix in _DOMAIN_SUFFIXES:
            if w.endswith(suffix) and len(w) > len(suffix):
                return w
    return None


def _is_public(host: str) -> bool:
    """Reject hosts that resolve to private/loopback/link-local/reserved/
    multicast IPs, so a scrape target can't be pointed at internal
    infrastructure (e.g. cloud metadata, localhost, RFC1918 ranges)."""
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def _robots_allows(url: str) -> bool:
    """Check robots.txt before scraping. Fails OPEN (returns True) if
    robots.txt itself is unreachable/malformed — per spec this is
    intentional: an unreachable robots.txt should not block a scrape."""
    parsed = urlparse(url)
    if not _is_public(parsed.netloc):
        return False
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = httpx.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt",
                      headers={"User-Agent": _UA}, timeout=5.0, follow_redirects=False)
        rp.parse(r.text.splitlines())
        return rp.can_fetch(_UA, url)
    except Exception:
        return True


def _scrape(url: str, emit) -> list[dict] | None:
    """Direct scrape of a competitor's own site. Checks robots.txt first,
    never follows redirects off the original domain, strips nav/script/
    style/header/footer tags, and caps content at 2000 chars per spec."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        emit("search", "bs4 not installed · skipping scrape")
        return None

    allowed_host = urlparse(url).netloc
    if not _is_public(allowed_host):
        emit("search", f"{allowed_host} resolves to a non-public address · skipping")
        return None

    if not _robots_allows(url):
        emit("search", f"robots.txt disallows {url} · skipping")
        return None

    try:
        emit("search", f"scraping {url}")
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=8.0, follow_redirects=False)
        r.raise_for_status()
        # Never follow links off the original domain — if a redirect
        # landed us somewhere else, treat that as a failed scrape rather
        # than silently returning content from an unintended site.
        if urlparse(str(r.url)).netloc != allowed_host:
            emit("search", "scrape redirected off-domain · aborting")
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["nav", "script", "style", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)[:2000]
        if not text:
            return None
        return [{"title": allowed_host.replace("www.", ""), "url": url, "content": text}]
    except httpx.HTTPStatusError as e:
        emit("search", f"scrape {url} → {e.response.status_code} · skipping")
        return None
    except httpx.HTTPError as e:
        emit("search", f"scrape {url} failed ({type(e).__name__})")
        return None
