# Tushar's Implementation Plan — Rivalyze Backend
## 3 Modules · 14 Hours · CodeClash 2026
### Self-contained. Follow this top-to-bottom. Nothing else needed.

---

## Context

You own the **Data Platform Pod** (with Mihir as equal partner) and the **Product Agent**. Your `search()` function is the critical path — every AI agent in the system calls it. Without it, nothing works.

**Three deliverables:**
1. `app/core/counters.py` — Redis credit counters (45 min, BUILD THIS FIRST)
2. `app/core/search_chain.py` — Frozen search chain: Tavily → Serper → scrape (2.5 hr, CRITICAL PATH)
3. `app/agents/product.py` — Product intelligence agent, cloned from discovery skeleton (2.5 hr)

---

## Files You Create

```
app/core/counters.py        ← MODULE 1 (you write)
app/core/search_chain.py    ← MODULE 2 (you write)
app/agents/product.py       ← MODULE 3 (you write)
tests/test_counters.py      ← you write
tests/test_search_chain.py  ← you write
tests/test_product_agent.py ← you write
```

**Files you import but do NOT write:**
- `app/core/cache.py` → Mihir's (`cache_get`, `cache_set`) — shim until ~Sat 13:00
- `app/core/llm_router.py` → Mihir hardens the POC version; `complete()` works from POC immediately
- `app/models.py` → Drashti's; `ProductIntel` is already defined in `member_packets/backend/models.py`
- `app/db/repository.py` → Dharvi's

---

## Prerequisites

```
# Add to requirements.txt if missing:
redis>=5.0
beautifulsoup4>=4.12
lxml>=5.0          # faster BS4 parser; html.parser also works as fallback
respx>=0.21        # for mocking httpx in tests

# Env vars needed (from Darshit's vault):
REDIS_URL=rediss://:KEY@NAME.redis.cache.windows.net:6380/0   # TWO s's in rediss:// = TLS!
TAVILY_API_KEY=tvly-...
SERPER_API_KEY=...
MOCK_MODE=0
```

---

## MODULE 1 — `app/core/counters.py`
### Saturday ~10:30 | 45 minutes | Build FIRST

**What it does:** Daily per-provider API credit counters in Redis.
Key format: `"credits:tavily:2026-07-04"`, `"credits:serper:2026-07-04"`, `"llm:groq:2026-07-04"`

**Golden rule: ALL failures are silent. Return 0. Log once. NEVER raise. NEVER block a run.**

### Complete File

```python
"""
app/core/counters.py
Daily per-provider API credit counters backed by Redis.
Key pattern: "credits:tavily:2026-07-04", "llm:groq:2026-07-04"
RULE: failures are silent — return 0, log once, never raise, never block a run.
Owner: Tushar
"""
import os
import logging
import datetime

logger = logging.getLogger(__name__)
_redis_client = None


def _get_client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = os.getenv("REDIS_URL")
    if not url:
        logger.warning("counters: REDIS_URL not set — all counter calls return 0")
        return None
    try:
        import redis
        _redis_client = redis.from_url(url, decode_responses=True, socket_timeout=3)
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        logger.warning("counters: Redis connection failed (%s) — returning 0", e)
        _redis_client = None
        return None


def counter_incr(name: str) -> int:
    """Increment counter 'name' by 1. Returns new total. Returns 0 on any failure."""
    client = _get_client()
    if client is None:
        return 0
    try:
        return int(client.incr(name))
    except Exception as e:
        logger.warning("counters: incr(%s) failed: %s", name, e)
        return 0


def counter_get(name: str) -> int:
    """Get current value of counter 'name'. Returns 0 on any failure."""
    client = _get_client()
    if client is None:
        return 0
    try:
        val = client.get(name)
        return int(val) if val is not None else 0
    except Exception as e:
        logger.warning("counters: get(%s) failed: %s", name, e)
        return 0


def today_key(provider: str) -> str:
    """Build today's counter key. Usage: counter_incr(today_key('tavily'))"""
    return f"credits:{provider}:{datetime.date.today().isoformat()}"
```

### tests/test_counters.py

```python
import sys, pytest

def _reload(monkeypatch, url):
    monkeypatch.setenv("REDIS_URL", url) if url else monkeypatch.delenv("REDIS_URL", raising=False)
    sys.modules.pop("app.core.counters", None)
    from app.core import counters
    return counters

def test_no_redis_url_incr(monkeypatch):
    c = _reload(monkeypatch, None)
    assert c.counter_incr("credits:tavily:2026-07-04") == 0

def test_no_redis_url_get(monkeypatch):
    c = _reload(monkeypatch, None)
    assert c.counter_get("credits:tavily:2026-07-04") == 0

def test_bad_url_incr(monkeypatch):
    c = _reload(monkeypatch, "rediss://localhost:1/0")
    assert c.counter_incr("credits:serper:2026-07-04") == 0

def test_today_key_format():
    import datetime
    sys.modules.pop("app.core.counters", None)
    from app.core.counters import today_key
    key = today_key("tavily")
    assert key == f"credits:tavily:{datetime.date.today().isoformat()}"
    assert key.count(":") == 2
```

### Verification Check (Sat ~11:15)

```python
# With REDIS_URL set:
from app.core.counters import counter_incr, counter_get, today_key
key = today_key("tavily")
v1 = counter_incr(key); v2 = counter_incr(key); v3 = counter_get(key)
print(v1, v2, v3)   # must be sequential ints; v3 == v2

# With REDIS_URL unset (verify silent failure):
import os; del os.environ["REDIS_URL"]
import importlib, app.core.counters; importlib.reload(app.core.counters)
from app.core.counters import counter_incr
print(counter_incr("anything"))   # must print 0, NOT raise
```

**After done → announce in group chat:** "counters.py ready — `counter_get`/`counter_incr` importable from `app.core.counters`" — Mihir needs this for his router hardening.

---

## MODULE 2 — `app/core/search_chain.py`
### Saturday ~11:30 | 2.5 hours | CRITICAL PATH — every agent depends on this

### FROZEN Signature — DO NOT CHANGE

```python
def search(query: str, emit) -> list[dict]:   # [{title, url, content}]
```

Every agent (discovery, news, product, reviews) imports this exact signature. Changing it breaks everyone.

### Before You Start: The Mihir Cache Shim

Mihir delivers `cache.py` around Sat 13:00. Until then, put this at the TOP of `search_chain.py`. It activates automatically via `ImportError` — no manual switching needed:

```python
try:
    from app.core.cache import cache_get, cache_set
except ImportError:
    # Temporary shim — delete this block once Mihir delivers cache.py
    _LOCAL: dict = {}
    def cache_get(key: str): return _LOCAL.get(key)
    def cache_set(key: str, value, ttl: int = 86400): _LOCAL[key] = value
```

### Search Flow Diagram

```
search(query, emit)
    │
    ├─ cache_get(sha256(normalized)[:16])
    │      ├─ HIT  → return cached, emit "cache hit"
    │      └─ MISS → continue
    │
    ├─ TAVILY_API_KEY set?
    │      ├─ YES → POST api.tavily.com/search
    │      │         ├─ 200 → counter_incr(tavily) → cache_set → return
    │      │         └─ error → emit "Tavily {code} · falling back to Serper"
    │      └─ NO  → skip
    │
    ├─ SERPER_API_KEY set?
    │      ├─ YES → POST google.serper.dev/search
    │      │         ├─ 200 → counter_incr(serper) → map organic[] → cache_set → return
    │      │         └─ error → emit "Serper error · falling back to scraper"
    │      └─ NO  → skip
    │
    ├─ domain word found in query? (e.g. "notion.so", "clickup.com")
    │      ├─ YES → check /robots.txt
    │      │         ├─ DISALLOWED → emit "robots.txt disallows" → skip
    │      │         └─ ALLOWED → httpx.get(url, UA=RivalyzeBot) → BS4 → 2000 chars → cache_set → return
    │      └─ NO  → skip
    │
    └─ emit "all providers exhausted" → return []
```

### Complete File

```python
"""
app/core/search_chain.py
FROZEN signature: search(query, emit) -> list[dict]  — do NOT change.
Search order: cache → Tavily → Serper → direct scrape → []
NO ddgs. Cache every non-empty result set. Missing env key = skip provider silently.
Owner: Tushar
"""
import os
import hashlib
import logging
import urllib.robotparser
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

try:
    from app.core.cache import cache_get, cache_set
except ImportError:
    _LOCAL: dict = {}
    def cache_get(key: str): return _LOCAL.get(key)
    def cache_set(key: str, value, ttl: int = 86400): _LOCAL[key] = value

from app.core.counters import counter_incr, today_key

logger = logging.getLogger(__name__)
_UA = "RivalyzeBot/0.1 (+hackathon demo)"
_DOMAIN_SUFFIXES = (".com", ".io", ".so", ".co", ".ai", ".app", ".net", ".org", ".dev", ".tech", ".ly")


def search(query: str, emit) -> list[dict]:
    """
    Primary search used by all agents. Returns [{title, url, content}].
    Returns [] on total failure — callers set low_signal, never crash.
    """
    normalized = query.lower().strip()
    cache_key = hashlib.sha256(normalized.encode()).hexdigest()[:16]

    cached = cache_get(cache_key)
    if cached is not None:
        emit("search", f'"{query}" · cache hit')
        return cached

    results = _tavily(query, emit)
    if results:
        return _store(cache_key, results)

    results = _serper(query, emit)
    if results:
        return _store(cache_key, results)

    domain = _extract_domain(query)
    if domain:
        results = _scrape(f"https://{domain}", emit)
        if results:
            return _store(cache_key, results)

    emit("search", f'"{query}" · all providers exhausted — returning empty')
    return []


def _store(key: str, results: list[dict]) -> list[dict]:
    if results:
        cache_set(key, results)
    return results


def _tavily(query: str, emit) -> list[dict]:
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return []
    try:
        emit("search", f'"{query}" · trying Tavily')
        r = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": 3},
            timeout=25.0,
        )
        r.raise_for_status()
        counter_incr(today_key("tavily"))
        return [{"title": x.get("title",""), "url": x.get("url",""), "content": x.get("content","")}
                for x in r.json().get("results", [])]
    except httpx.HTTPStatusError as e:
        emit("search", f"Tavily {e.response.status_code} · falling back to Serper")
        return []
    except Exception as e:
        emit("search", f"Tavily error ({type(e).__name__}) · falling back to Serper")
        return []


def _serper(query: str, emit) -> list[dict]:
    # CRITICAL: Serper uses "link" (not "url") and "snippet" (not "content")
    api_key = os.getenv("SERPER_API_KEY")
    if not api_key:
        return []
    try:
        emit("search", f'"{query}" · trying Serper')
        r = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 3},
            timeout=20.0,
        )
        r.raise_for_status()
        counter_incr(today_key("serper"))
        return [{"title": x.get("title",""), "url": x.get("link",""), "content": x.get("snippet","")}
                for x in r.json().get("organic", [])]
    except httpx.HTTPStatusError as e:
        emit("search", f"Serper {e.response.status_code} · falling back to scraper")
        return []
    except Exception as e:
        emit("search", f"Serper error ({type(e).__name__}) · falling back to scraper")
        return []


def _extract_domain(query: str) -> str | None:
    """Return the first domain-looking word in query (has known TLD suffix), or None."""
    for word in query.split():
        w = word.strip("\"'.,;:()")
        for suffix in _DOMAIN_SUFFIXES:
            if w.endswith(suffix) and len(w) > len(suffix):
                return w
    return None


def _robots_allows(url: str) -> bool:
    """Check robots.txt. Returns True (fail open) on any error."""
    parsed = urlparse(url)
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
    try:
        rp.read()
        return rp.can_fetch(_UA, url)
    except Exception:
        return True   # fail open: if robots.txt unreachable, proceed


def _scrape(url: str, emit) -> list[dict]:
    """Direct scrape. Checks robots.txt first. Never follows off-domain links."""
    if not _robots_allows(url):
        emit("search", f"robots.txt disallows {url} · skipping")
        return []
    allowed_host = urlparse(url).netloc
    try:
        emit("search", f"scraping {url}")
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=8.0, follow_redirects=True)
        r.raise_for_status()
        if urlparse(str(r.url)).netloc != allowed_host:
            emit("search", f"scrape redirected off-domain · aborting")
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["nav", "script", "style", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)[:2000]
        return [{"title": allowed_host.replace("www.", ""), "url": url, "content": text}]
    except httpx.HTTPStatusError as e:
        emit("search", f"scrape {url} → {e.response.status_code} · skipping")
        return []
    except Exception as e:
        emit("search", f"scrape {url} failed ({type(e).__name__})")
        return []
```

### tests/test_search_chain.py

```python
import pytest, hashlib, respx, httpx
from unittest.mock import MagicMock

@pytest.fixture(autouse=True)
def patch_deps(monkeypatch):
    store = {}
    monkeypatch.setattr("app.core.search_chain.cache_get", lambda k: store.get(k))
    monkeypatch.setattr("app.core.search_chain.cache_set", lambda k,v,ttl=86400: store.update({k:v}))
    monkeypatch.setattr("app.core.search_chain.counter_incr", lambda n: 1)
    return store

@pytest.fixture
def emit(): return MagicMock()


# Test 1: Cache hit → no HTTP calls at all
def test_cache_hit(patch_deps, emit, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "key")
    cached = [{"title":"C","url":"https://c.com","content":"c"}]
    key = hashlib.sha256("notion pricing".encode()).hexdigest()[:16]
    patch_deps[key] = cached
    with respx.mock(assert_all_called=False) as m:
        m.post("https://api.tavily.com/search").mock(side_effect=AssertionError("Tavily must not be called"))
        from app.core.search_chain import search
        result = search("Notion Pricing", emit)   # normalized = "notion pricing" → same key
    assert result == cached


# Test 2: Tavily success → result cached, counter incremented
@respx.mock
def test_tavily_success(patch_deps, emit, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "key")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    incr_calls = []
    monkeypatch.setattr("app.core.search_chain.counter_incr", lambda n: incr_calls.append(n) or 1)
    respx.post("https://api.tavily.com/search").mock(
        return_value=httpx.Response(200, json={"results":[{"title":"T","url":"https://t.com","content":"tc"}]}))
    from importlib import reload; import app.core.search_chain as sc; reload(sc)
    result = sc.search("Notion pricing July 2026", emit)
    assert result[0]["url"] == "https://t.com"
    assert any("tavily" in n for n in incr_calls)
    key = hashlib.sha256("notion pricing july 2026".encode()).hexdigest()[:16]
    assert patch_deps.get(key) is not None


# Test 3: Tavily 4xx → Serper called; verify Serper field mapping (link→url, snippet→content)
@respx.mock
def test_tavily_fails_serper_called(patch_deps, emit, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "key"); monkeypatch.setenv("SERPER_API_KEY", "skey")
    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(401))
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json={"organic":[{"title":"S","link":"https://s.com","snippet":"sc"}]}))
    from importlib import reload; import app.core.search_chain as sc; reload(sc)
    result = sc.search("ClickUp features 2026", emit)
    assert result[0]["url"] == "https://s.com"    # Serper "link" → "url"
    assert result[0]["content"] == "sc"           # Serper "snippet" → "content"


# Test 4: Both fail + no domain word in query → return []
@respx.mock
def test_both_fail_no_domain_empty(emit, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY","key"); monkeypatch.setenv("SERPER_API_KEY","skey")
    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(500))
    respx.post("https://google.serper.dev/search").mock(return_value=httpx.Response(503))
    from importlib import reload; import app.core.search_chain as sc; reload(sc)
    assert sc.search("latest news funding July 2026", emit) == []


# Test 5: Domain in query + robots.txt allows → scraped, nav/script stripped
@respx.mock
def test_scrape_strips_nav_script(patch_deps, emit, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY","key"); monkeypatch.setenv("SERPER_API_KEY","skey")
    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(500))
    respx.post("https://google.serper.dev/search").mock(return_value=httpx.Response(500))
    respx.get("https://notion.so/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n"))
    respx.get("https://notion.so").mock(
        return_value=httpx.Response(200, text="<html><body><nav>SKIP</nav><script>SKIP</script><p>Pro $10/month</p></body></html>"))
    from importlib import reload; import app.core.search_chain as sc; reload(sc)
    result = sc.search("notion.so pricing plans", emit)
    assert len(result) == 1
    assert "SKIP" not in result[0]["content"]
    assert "Pro $10" in result[0]["content"]
```

### Verification Check (Sat ~14:15)

```python
from app.core.search_chain import search
log = []
emit = lambda a, m: log.append(m)

# Normalization + cache hit check
r1 = search("Notion pricing plans", emit)
r2 = search("Notion PRICING plans  ", emit)   # different caps/spaces → same SHA-256 key
assert r1 == r2
assert any("cache hit" in m for m in log)

# Serper field mapping check (remove Tavily key to force Serper path)
import os; os.environ.pop("TAVILY_API_KEY", None)
result = search("ClickUp comparison 2026", emit)
for r in result:
    assert r["url"].startswith("http"), f"Empty URL from Serper: {r}"
```

**After done → announce:** "search(query, emit) is live. Import from `app.core.search_chain`." Sheel, Virat, Mihir all unblocked.

---

## MODULE 3 — `app/agents/product.py`
### Saturday ~17:30 | 2.5 hours | Clone from discovery.py skeleton

### What to clone
Read `poc_vertical_slice/app/agents/discovery.py` first. Same structure:
- search → build corpus → LLM prompt → `complete()` → typed output → low_signal fallback
- `try/except` around `complete()` → return `_low_signal(competitor)`, never raise

### Key Facts Before Writing
- `ProductIntel` is in `app/models.py` — do NOT redefine, just import
- `complete("extract", prompt, ProductIntel, emit)` returns `(validated_instance, lane_str)`
- After `complete()`: **always stamp** `result.competitor = competitor` (prevents LLM capitalization drift — e.g. "clickup" instead of "ClickUp")
- `company: str = ""` with default → backward-compatible; orchestrator passes `company=state["company"]`

### The LLM System Prompt (copy exactly — never weaken the CAPS rules)

```
Extract pricing and product positioning for {competitor}.
pricing_tiers are PLAIN STRINGS like "Pro $12/seat: AI included" —
NEVER nested objects (wrong example: {"tier":"Pro","price":12}).
advantages = angles {company} can use AGAINST them, from the corpus only.
Every source in "sources" must be a real URL from the corpus — never invent URLs.
ONLY JSON: {"pricing_tiers":[],"recent_features":[],"positioning":"","advantages":[],"sources":[]}
```

### Complete File

```python
"""
app/agents/product.py
Product intelligence agent — pricing tiers, features, positioning per competitor.
Cloned from: poc_vertical_slice/app/agents/discovery.py (same search→LLM pattern)
Spec: PART 1 of member_packets/backend/sheel_prompt.txt
Output: list[ProductIntel] as dicts — one per competitor, always, never raises.
Owner: Tushar
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.models import ProductIntel
from app.core.search_chain import search
from app.core.llm_router import complete

logger = logging.getLogger(__name__)

_MONTH = datetime.now().strftime("%B %Y")   # "July 2026" — module-level, computed once
_CORPUS_CAP = 5000
_LOW_SIGNAL_THRESHOLD = 300

_SYSTEM = (
    'Extract pricing and product positioning for {competitor}. '
    'pricing_tiers are PLAIN STRINGS like "Pro $12/seat: AI included" — '
    'NEVER nested objects (wrong example: {{"tier":"Pro","price":12}}). '
    'advantages = angles {company} can use AGAINST them, from the corpus only. '
    'Every source in "sources" must be a real URL from the corpus — never invent URLs. '
    'ONLY JSON: {{"pricing_tiers":[],"recent_features":[],"positioning":"","advantages":[],"sources":[]}}'
)


def run(competitors: list, emit, company: str = "") -> list[dict]:
    """
    Extract product intel for each competitor. Returns list[ProductIntel] as dicts.
    Never raises.
    """
    results = []
    for item in competitors:
        name = item if isinstance(item, str) else item.get("name", str(item))
        intel = _process(name, company, emit)
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
        result.competitor = competitor    # stamp from known value — never trust LLM capitalization
        emit("product", f"{competitor} · {len(result.pricing_tiers)} tiers via {lane}")
        return result
    except Exception as e:
        emit("product", f"low signal: extraction failed for {competitor}: {type(e).__name__}")
        logger.warning("product: failed for %s: %s", competitor, e)
        return _low_signal(competitor)


def _build_corpus(competitor: str, company: str, emit) -> str:
    queries = [f"{competitor} pricing plans {_MONTH}"]
    if company:
        queries.append(f"{competitor} vs {company} comparison {_MONTH}")  # comparison articles are data-rich
    queries.append(f"{competitor} new features product update 2026")

    seen_urls: set[str] = set()
    parts: list[str] = []
    for q in queries:
        for item in search(q, emit):
            url = item.get("url", "")
            if url and url in seen_urls:
                continue
            seen_urls.add(url)
            parts.append(f"{item.get('title','')}\n{item.get('content','')}\nSOURCE: {url}\n")

    return "\n".join(parts)[:_CORPUS_CAP]


def _low_signal(competitor: str) -> ProductIntel:
    return ProductIntel(competitor=competitor, pricing_tiers=[], recent_features=[],
                        positioning="", advantages=[], sources=[], low_signal=True)
```

### tests/test_product_agent.py

```python
import pytest
from unittest.mock import MagicMock, patch

@pytest.fixture
def emit(): return MagicMock()

@pytest.fixture(autouse=True)
def fat_search(monkeypatch):
    """Default: search returns enough content to clear the 300-char threshold."""
    monkeypatch.setattr("app.agents.product.search", lambda q, e: [
        {"title":"Test","url":"https://coda.io/pricing","content":"Pro $12/seat AI included "*25}
    ])

def _good_intel(name="Coda"):
    from app.models import ProductIntel
    return ProductIntel(competitor=name, pricing_tiers=["Pro $12/seat: AI included"],
                        recent_features=["AI formulas"], positioning="docs-as-apps",
                        advantages=["simpler onboarding"], sources=["https://coda.io/pricing"])

def test_returns_list_of_dicts(emit):
    from app.agents.product import run
    with patch("app.agents.product.complete", return_value=(_good_intel(), "mock")):
        result = run(["Coda"], emit)
    assert isinstance(result, list) and len(result) == 1
    assert result[0]["competitor"] == "Coda"

def test_pricing_tiers_are_plain_strings(emit):
    """#1 correctness check — must be list[str], never list[dict]."""
    from app.agents.product import run
    with patch("app.agents.product.complete", return_value=(_good_intel(), "mock")):
        result = run(["Coda"], emit)
    for tier in result[0]["pricing_tiers"]:
        assert isinstance(tier, str), f"Got nested object: {tier}"

def test_low_signal_on_thin_corpus(emit, monkeypatch):
    """Empty search → corpus < 300 chars → low_signal=True, complete() never called."""
    monkeypatch.setattr("app.agents.product.search", lambda q, e: [])
    from app.agents.product import run
    with patch("app.agents.product.complete") as mc:
        result = run(["Ghost Corp"], emit)
        mc.assert_not_called()
    assert result[0]["low_signal"] is True

def test_low_signal_on_llm_failure(emit):
    """complete() raises → low_signal=True, exception does NOT propagate."""
    from app.agents.product import run
    with patch("app.agents.product.complete", side_effect=RuntimeError("all lanes exhausted")):
        result = run(["BadCorp"], emit)
    assert result[0]["low_signal"] is True
    assert result[0]["competitor"] == "BadCorp"

def test_competitor_from_dict(emit):
    """Competitors can be dicts with 'name' key (LangGraph orchestrator format)."""
    from app.agents.product import run
    with patch("app.agents.product.complete", return_value=(_good_intel("ClickUp"), "mock")):
        result = run([{"name": "ClickUp", "category": "direct"}], emit)
    assert result[0]["competitor"] == "ClickUp"
```

### Verification Check (Sat ~20:00)

```python
from app.agents.product import run
emit = lambda a, m: print(f"[{a}] {m}")

# Check 1: pricing_tiers are plain strings, not dicts
result = run(["Notion", "Coda"], emit, company="YourStartup")
for r in result:
    for tier in r["pricing_tiers"]:
        assert isinstance(tier, str), f"NESTING BUG: {tier}"

# Check 2: unknown company → low_signal, no crash
result = run(["ASDFJKL_FAKE_CORP_999"], emit)
assert result[0]["low_signal"] is True
print("All checks passed")
```

**After done → announce:** "product.py done — outputs list[ProductIntel] dicts. Gati can consume. Krutarth's h2h will show pricing_tiers."

---

## Dependency Timeline

| Time | Event | What To Do |
|---|---|---|
| Sat 10:30 | Build start | `cache.py` doesn't exist yet — shim in search_chain.py is active automatically |
| Sat 11:00 | counters.py done | Tell Mihir: "counter_get/counter_incr importable from app.core.counters" |
| Sat 13:00 | Mihir delivers cache.py | `try` branch fires automatically. Verify: `from app.core.cache import cache_get` works. |
| Sat 14:00 | search_chain.py done | Announce in pod. All agents can now call `search()`. |
| Sat 17:30 | product.py build start | Need `app/models.py` from Drashti. If not ready: copy `member_packets/backend/models.py` → `app/models.py` temporarily |
| Sat 20:00 | product.py done | Announce. Gati's merge_node starts consuming your output. |
| Sun 08:30 | Credit report | Run credit_report.py, post numbers in group |
| Sun 10:30 | TC-N01 drill | With Rushabh: set `TAVILY_API_KEY=REVOKED` on Azure → watch Serper take over live |
| Sun 11:00 | URL integrity | With Dharvi: HEAD-request all evidence URLs, find 404s, report before 13:30 rehearsal |

---

## Sunday Scripts

### Credit Report (run at 08:30, post in group)

```python
# scripts/credit_report.py
import datetime
from app.core.counters import counter_get

today = datetime.date.today().isoformat()
for provider in ["tavily", "serper", "groq", "gemini", "cerebras", "openrouter"]:
    count = counter_get(f"credits:{provider}:{today}")
    print(f"  {provider:12s}: {count:5d}")
```

### TC-N01 Drill Steps (Sat 10:30 with Rushabh)

1. Start a fresh `/api/v1/analyze` call — watch events show `"trying Tavily"`
2. Set `TAVILY_API_KEY=REVOKED` in Azure App Service → Configuration (wrong key triggers 401, no restart needed)
3. Next search call: Tavily 401 → emit `"Tavily 401 · falling back to Serper"` → Serper called
4. **Pass:** run completes, events show Serper taking over, `status = "completed"`

### URL Integrity Check (11:00–13:00 with Dharvi)

```python
import httpx
# Get evidence_rows from Dharvi's repository.get_evidence() — coordinate the exact signature
dead = []
for row in evidence_rows:
    url = row.get("url", "")
    if not url.startswith("http"):
        dead.append((row["id"], "no URL")); continue
    try:
        r = httpx.head(url, timeout=5, follow_redirects=True)
        if r.status_code >= 400:
            dead.append((row["id"], str(r.status_code)))
    except Exception as e:
        dead.append((row["id"], str(e)))
print(f"Dead URLs: {len(dead)}")
for rid, reason in dead: print(f"  {rid}: {reason}")
```

Report 404s to Drashti before the 13:30 rehearsal. Judges click these URLs live on stage.

---

## Definition of Done — 3 Checks

| Check | How to verify |
|---|---|
| Repeat query = 0 external calls | `search("X", emit)` twice → 2nd call emits "cache hit", no HTTP to Tavily/Serper |
| Tavily revoked mid-run → Serper visible on screen | TC-N01 drill Sunday 10:30 with Rushabh passes |
| Product tiers render un-nested on Krutarth's h2h | `pricing_tiers: ["Pro $12/seat: AI included"]` not `[{"tier":"Pro",...}]` |

---

## Critical Gotchas — Read These Before Starting

| # | Gotcha | Why it matters |
|---|---|---|
| 1 | **Serper uses `"link"` not `"url"`, `"snippet"` not `"content"`** | Getting this wrong = empty URLs silently, TC-N01 appears to work but results are empty |
| 2 | **`rediss://` has TWO s characters** (TLS, port 6380) | One `s` = plaintext, Azure Redis will refuse the connection |
| 3 | **Robots.txt fails open** — return `True` on any error | Intentional: if robots.txt is unreachable, proceed with scraping |
| 4 | **Always stamp `result.competitor = competitor` after `complete()`** | Prevents LLM returning "click up" instead of "ClickUp" |
| 5 | **Cache normalization** — `"Notion"`, `"notion"`, `"  NOTION  "` → same SHA-256 key | The Tier-4 `"NOTION "` test will fail if you forget `.lower().strip()` |
| 6 | **`complete()` returns a TUPLE** — `result, lane = complete(...)` | `result` is already a validated Pydantic instance; don't re-validate |
| 7 | **`_MONTH` is module-level** — computed once at import | Don't compute inside the loop; "July 2026" is correct for the whole hackathon |
| 8 | **NO ddgs** — DuckDuckGo is explicitly banned | Don't import it, don't use it |

---

## Final CI Gate — Run After Each Module

```bash
MOCK_MODE=1 pytest -q
```

This is exactly what `ci.yml` runs on every PR. Green = ready to merge.

---

## Collaboration Map

| Person | Why | When |
|---|---|---|
| **Mihir** (pair partner) | You import his `cache_get`/`cache_set` · He uses your `counter_get` · Review each other's merges | All day Saturday |
| **Drashti** | `app/models.py` has `ProductIntel` — if not ready, copy from member_packets temporarily | Sat morning |
| **Gati** | Her merge_node consumes your `state["product_results"]` — each `ProductIntel.sources` → EvidenceRows | Sat evening |
| **Krutarth** | His Dashboard h2h shows your `pricing_tiers` — must be plain strings or his UI breaks | Sat night |
| **Rushabh** (QA) | TC-N01 live Tavily-revoke drill | Sun 10:30 |
| **Dharvi** | URL integrity HEAD-request sweep on hero evidence rows | Sun 11:00 |
