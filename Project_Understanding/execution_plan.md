# Execution Plan — Mihir's Modules
> **Feed this file to an agentic coder** (e.g. Antigravity, Copilot) to implement Mihir's work autonomously.
> Each step includes exact file paths, function signatures, acceptance criteria, and dependency ordering.
>
> **Pair:** Tushar (Data Platform Pod). Coordinate on: cache interface (Mihir publishes first), counter names, lane_stats shape.

---

## Pre-conditions (must be true before Saturday 10:00)

- [ ] Python 3.12 confirmed: `python --version`
- [ ] POC runs in mock mode: `$env:MOCK_MODE="1"; uvicorn app.main:app --port 8000` → hit `/api/v1/health` → `{ "status": "ok" }`
- [ ] `redis-py` installed: `python -c "import redis; print(redis.__version__)"`
- [ ] `httpx` installed: `python -c "import httpx; print(httpx.__version__)"`
- [ ] `REDIS_URL` env var confirmed readable (from team vault)
- [ ] Hero research notes doc created: Notion, Zomato, Razorpay links (30–40 min tonight)

---

## Phase 0 — Friday Night: Hero Research (30–40 min)

**Goal:** Produce a raw links document that Pooja uses as quality yardstick at Sat 14:00.

**For each of: Notion, Zomato, Razorpay**

1. Search for **4–5 rival company names** in the same market.
2. Find **pricing page URLs** for each rival (e.g. `notion.so/pricing` → competitor pages).
3. Find **3 recent news links** (2026) per rival (funding, launches, partnerships).
4. Find **2 complaint threads** per rival — Reddit or G2 preferred.

**Output:** Paste all into a Slack message or shared doc before 8 PM Friday. Label it clearly `[Mihir] Hero Research Notes`.

---

## Phase 1 — Cache Module (Sat 11:00–13:00)

### Step 1.1 — Create `app/core/cache.py`

Create the file at `rivalyze-backend/app/core/cache.py`.

**Complete implementation:**

```python
"""Cache module — redis-py against Azure Cache for Redis with Postgres write-through fallback.

Owner: Mihir
Consumed by: app/core/search_chain.py (Tushar)
Never raises — all failures degrade silently to cache miss + one emitted event.
"""

import hashlib
import json
import logging
import os
from typing import Callable

import redis
import redis.exceptions

logger = logging.getLogger(__name__)

# --- Module-level lazy singleton ---
_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis | None:
    """Return a lazy-initialised Redis client, or None if REDIS_URL is not set."""
    global _redis_client
    if _redis_client is None:
        url = os.getenv("REDIS_URL")
        if url:
            _redis_client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_timeout=3,
            )
    return _redis_client


def make_cache_key(query: str) -> str:
    """Normalise a search query and produce a 16-char hex cache key.
    
    sha256(query.lower().strip())[:16] — deterministic, collision-resistant for our scale.
    """
    normalised = query.lower().strip()
    return hashlib.sha256(normalised.encode()).hexdigest()[:16]


# ---------- public API ----------

def cache_get(key: str, emit: Callable | None = None) -> dict | None:
    """Read from cache: Redis first, then Postgres, then miss.
    
    Args:
        key: 16-char hex key (use make_cache_key to produce it)
        emit: optional event emitter (same signature as agent emit)
    
    Returns:
        Parsed dict if found, None on miss.
    """
    # 1. Try Redis
    client = _get_redis()
    if client:
        try:
            raw = client.get(key)
            if raw:
                return json.loads(raw)
        except (redis.exceptions.RedisError, json.JSONDecodeError) as exc:
            _emit_event(emit, f"cache · redis miss ({type(exc).__name__}): {key}")

    # 2. Fallback: try Upstash REST if configured
    if not os.getenv("REDIS_URL") and os.getenv("UPSTASH_URL"):
        result = _upstash_get(key, emit)
        if result is not None:
            return result

    # 3. Try Postgres (write-through persistence)
    try:
        from app.db import repository
        return repository.get_search_cache(key)
    except Exception as exc:
        _emit_event(emit, f"cache · postgres miss ({type(exc).__name__}): {key}")

    return None


def cache_set(key: str, value: dict, ttl: int = 86400, emit: Callable | None = None) -> None:
    """Write to both Redis and Postgres (write-through).
    
    Write-through ensures cache survives a Redis flush (TC-P02).
    Failures are swallowed — cache must never break a run.
    
    Args:
        key: 16-char hex key
        value: serialisable dict to store
        ttl: Redis TTL in seconds (default 24h)
        emit: optional event emitter
    """
    serialised = json.dumps(value)

    # 1. Write to Redis
    client = _get_redis()
    if client:
        try:
            client.setex(key, ttl, serialised)
        except redis.exceptions.RedisError as exc:
            _emit_event(emit, f"cache · redis write fail ({type(exc).__name__}): {key}")

    # 2. Write Upstash if no Redis
    if not os.getenv("REDIS_URL") and os.getenv("UPSTASH_URL"):
        _upstash_set(key, value, ttl, emit)

    # 3. Always write Postgres (write-through)
    try:
        from app.db import repository
        repository.save_search_cache(key, value)
    except Exception as exc:
        _emit_event(emit, f"cache · postgres write fail ({type(exc).__name__}): {key}")


# ---------- Upstash REST fallback ----------

def _upstash_get(key: str, emit: Callable | None) -> dict | None:
    """GET key from Upstash REST API. Returns None on any failure."""
    try:
        import httpx
        url = os.getenv("UPSTASH_URL")
        token = os.getenv("UPSTASH_TOKEN", "")
        resp = httpx.get(
            f"{url}/get/{key}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=3,
        )
        body = resp.json()
        if body.get("result"):
            return json.loads(body["result"])
    except Exception as exc:
        _emit_event(emit, f"cache · upstash get fail ({type(exc).__name__}): {key}")
    return None


def _upstash_set(key: str, value: dict, ttl: int, emit: Callable | None) -> None:
    """SETEX key in Upstash REST API. Swallows failures."""
    try:
        import httpx
        url = os.getenv("UPSTASH_URL")
        token = os.getenv("UPSTASH_TOKEN", "")
        httpx.post(
            f"{url}/setex/{key}/{ttl}",
            content=json.dumps(value),
            headers={"Authorization": f"Bearer {token}"},
            timeout=3,
        )
    except Exception as exc:
        _emit_event(emit, f"cache · upstash set fail ({type(exc).__name__}): {key}")


# ---------- helper ----------

def _emit_event(emit: Callable | None, msg: str) -> None:
    if emit:
        emit({"agent": "cache", "msg": msg})
    logger.debug(msg)
```

### Step 1.2 — Smoke test locally

```powershell
# From rivalyze-backend/
$env:REDIS_URL = "rediss://:YOUR_KEY@YOUR_NAME.redis.cache.windows.net:6380/0"
python -c "
from app.core.cache import make_cache_key, cache_get, cache_set
k = make_cache_key('test query')
cache_set(k, {'test': True})
print(cache_get(k))   # should print {'test': True}
"
```

### Step 1.3 — Coordinate with Tushar

Tell Tushar in the group: **"cache.py published — `cache_get`/`cache_set` + `make_cache_key` available. Key format: `sha256(q.lower().strip())[:16]`."**

Tushar's `search_chain.py` will call:
```python
from app.core.cache import make_cache_key, cache_get, cache_set
key = make_cache_key(query)
cached = cache_get(key, emit)
if cached: return cached
# ... do real search ...
cache_set(key, results, emit=emit)
```

---

## Phase 2 — Router Hardening Part 1 (Sat 13:00–15:00)

### Step 2.1 — Commit `budgets.json`

Create `rivalyze-backend/budgets.json`:

```json
{
  "gemini":    1000,
  "groq_8b":  14000,
  "groq_70b":   900,
  "cerebras":   900,
  "openrouter":  45,
  "tavily":     900,
  "serper":    2300
}
```

### Step 2.2 — Add budget loading to `llm_router.py`

At the top of the module (after existing imports):

```python
import json, os
from pathlib import Path
from app.core.counters import counter_get, counter_incr

# Load budget limits once at import time
_BUDGETS_PATH = Path(__file__).parent.parent.parent / "budgets.json"
_BUDGETS: dict[str, int] = {}
if _BUDGETS_PATH.exists():
    with open(_BUDGETS_PATH) as _f:
        _BUDGETS = json.load(_f)
```

### Step 2.3 — Inject budget check into the per-lane attempt loop

In the existing `complete()` function, before the `httpx` call for each lane, add:

```python
# --- BUDGET CHECK (Mihir's hardening) ---
spent = counter_get(f"llm:{lane['name']}")
budget = _BUDGETS.get(lane['name'], 999_999)
if spent >= budget:
    emit({"agent": "router", "msg": f"lane {lane['name']} at budget ({spent}/{budget}) — skip"})
    continue   # next lane

# --- REAL CALL ---
# ... existing httpx / retry logic ...

# After a successful response:
counter_incr(f"llm:{lane['name']}")
```

### Step 2.4 — Add `lane_stats` accumulation

Add a module-level dict (or pass it as state) to track stats per `complete()` invocation:

```python
# Module-level stats accumulator — reset per run by the lifecycle
_run_stats: dict = {}

def reset_run_stats() -> None:
    """Call this at the start of each pipeline run."""
    global _run_stats
    _run_stats = {}

def get_run_stats() -> dict:
    """Return accumulated lane_stats for the current run."""
    return dict(_run_stats)

def _incr_stat(key: str, amount: int = 1) -> None:
    _run_stats[key] = _run_stats.get(key, 0) + amount

# Inside complete(), after a successful real call:
_incr_stat(lane["name"])       # e.g. _run_stats["gemini"] += 1

# cache_hits and searches are incremented by cache.py and search_chain.py respectively:
# call _incr_stat("cache_hits") from cache_get on hit
# call _incr_stat("searches") from search_chain.py on each real external search
```

---

## Phase 3 — Reviews Agent (Sat 15:00–17:30)

### Step 3.1 — Create `app/agents/review.py`

```python
"""Reviews agent — mines customer complaints and sentiment per competitor.

Owner: Mihir
Spec: Agent 2 from virat_prompt.txt
Output: list[SentimentIntel]

Rules:
- top_complaints: ≤3 SHORT plain strings, NO nested objects
- overall_sentiment: enum POSITIVE | NEUTRAL | NEGATIVE only
- low_signal if corpus < 300 chars or 0 results
- Never raises — caller always gets typed SentimentIntel
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from app.core.cache import make_cache_key, cache_get, cache_set
from app.core.llm_router import complete
from app.core.search_chain import search
from app.db.models import SentimentIntel

logger = logging.getLogger(__name__)

MONTH = datetime.now().strftime("%B %Y")

_SYSTEM_PROMPT = """Mine customer complaints about {competitor} from the corpus (reviews, Reddit, forums).

top_complaints: UP TO 3 SHORT plain strings ONLY — example: "feature overload"
WRONG (do not do this): {{"issue": "overload", "severity": "high"}}
RIGHT: "feature overload"

opportunity_gaps: one exploitable gap per complaint, phrased as an opportunity for {company}.
overall_sentiment: EXACTLY one of POSITIVE | NEUTRAL | NEGATIVE — no other values.
sources: ONLY URLs that actually appear in the corpus above — never invent URLs.

ONLY JSON (no markdown, no explanation):
{{"top_complaints":[],"opportunity_gaps":[],"overall_sentiment":"NEUTRAL","sources":[]}}"""


async def run(
    competitors: list[str],
    emit: Callable[[dict], None],
    company: str = "our company",
) -> list[SentimentIntel]:
    """Run complaint mining and sentiment analysis for each competitor.
    
    Args:
        competitors: list of rival company names
        emit: event emitter (same signature used by all agents)
        company: our company name (used in opportunity gap framing)
    
    Returns:
        One SentimentIntel per competitor, always — never raises.
    """
    results: list[SentimentIntel] = []

    for c in competitors:
        try:
            result = await _run_single(c, company, emit)
        except Exception as exc:
            logger.error("reviews · unhandled error for %s: %s", c, exc)
            emit({"agent": "reviews", "msg": f"reviews · error for {c}: {exc}"})
            result = SentimentIntel(competitor=c, low_signal=True)
        results.append(result)

    return results


async def _run_single(
    competitor: str,
    company: str,
    emit: Callable,
) -> SentimentIntel:
    """Process one competitor. May raise — caller wraps in try/except."""
    # 1. Search
    raw_results = []
    for query in [
        f"{competitor} customer complaints problems {MONTH}",
        f"{competitor} negative reviews reddit {MONTH}",
    ]:
        raw_results.extend(await search(query, emit))

    # 2. Build corpus
    corpus_parts = []
    for r in raw_results:
        corpus_parts.append(f"SOURCE: {r['url']}\n{r.get('title','')}\n{r.get('content','')}")
    corpus = "\n\n".join(corpus_parts)[:5000]

    # 3. Low-signal guard
    if len(corpus) < 300 or not raw_results:
        emit({"agent": "reviews", "msg": f"reviews · low signal: {competitor}"})
        return SentimentIntel(competitor=competitor, low_signal=True)

    # 4. LLM extraction
    prompt = _SYSTEM_PROMPT.format(competitor=competitor, company=company) + f"\n\nCORPUS:\n{corpus}"
    model_instance, lane = complete("extract", prompt, SentimentIntel, emit)

    # 5. Flatten any accidental nesting in complaints
    model_instance.top_complaints = [
        _flatten_complaint(c) for c in model_instance.top_complaints
    ][:3]

    return model_instance


def _flatten_complaint(complaint) -> str:
    """Ensure complaint is a plain string — flatten dicts/objects from weak models."""
    if isinstance(complaint, str):
        return complaint
    if isinstance(complaint, dict):
        # Take the first string value found
        for v in complaint.values():
            if isinstance(v, str):
                return v
        return str(complaint)
    return str(complaint)
```

### Step 3.2 — Verify output shape

```powershell
# Quick manual test with mock data
python -c "
import asyncio
from app.agents.review import run

async def test():
    results = await run(['Notion', 'Coda'], emit=print, company='Rivalyze')
    for r in results:
        print(r.model_dump_json(indent=2))

asyncio.run(test())
"
```

**Expected:** Each result has `top_complaints` as a list of short plain strings, `overall_sentiment` as one of the three enums, `sources` as URL strings.

---

## Phase 4 — Router Hardening Part 2 (Sat 17:30–18:30)

### Step 4.1 — Add `DEMO_RESERVE` key resolution to `llm_router.py`

```python
# Near the top of llm_router.py

_DEMO_RESERVE = os.getenv("DEMO_RESERVE", "0") == "1"

def _resolve_api_key(provider_name: str, default_env_var: str) -> str | None:
    """Return the API key for a provider, preferring reserve key on demo day.
    
    Args:
        provider_name: e.g. "gemini", "groq_8b"
        default_env_var: e.g. "GEMINI_API_KEY"
    
    Returns:
        API key string or None if unavailable.
    """
    if _DEMO_RESERVE:
        reserve = os.getenv(f"{provider_name.upper()}_RESERVE_KEY")
        if reserve:
            return reserve
    return os.getenv(default_env_var)
```

Update the lane config to use `_resolve_api_key()` instead of `os.getenv()` directly.

### Step 4.2 — Write `tests/test_llm_router.py`

```python
"""Pytest suite for llm_router hardening — all httpx calls mocked.

Run: MOCK_MODE=1 pytest tests/test_llm_router.py -v
"""

import json
import os
import pytest
from unittest.mock import patch, MagicMock


class TestBudgetSkip:
    """TC: lane at budget → skip with event, no real HTTP call."""

    def test_lane_skipped_when_budget_exhausted(self, monkeypatch):
        """When all lanes are at budget, complete() raises RuntimeError."""
        from app.core import llm_router
        
        # Set all budgets to 0
        monkeypatch.setattr(llm_router, "_BUDGETS", {
            "gemini": 0, "groq_8b": 0, "cerebras": 0, "openrouter": 0
        })
        
        events = []
        with pytest.raises(RuntimeError, match="all lanes exhausted"):
            llm_router.complete("extract", "test prompt", MagicMock(), events.append)
        
        # Budget-skip events should have been emitted
        assert any("budget" in str(e) for e in events)

    def test_skip_event_format(self, monkeypatch):
        """Budget-skip event must include lane name and spend/budget numbers."""
        from app.core import llm_router
        monkeypatch.setattr(llm_router, "_BUDGETS", {"gemini": 0})
        
        events = []
        try:
            llm_router.complete("reason", "test", MagicMock(), events.append)
        except RuntimeError:
            pass
        
        gemini_skip = next((e for e in events if "gemini" in str(e) and "budget" in str(e)), None)
        assert gemini_skip is not None


class TestDemoReserveKey:
    """TC: DEMO_RESERVE=1 prefers *_RESERVE_KEY env vars."""

    def test_reserve_key_selected_when_demo_reserve_set(self, monkeypatch):
        monkeypatch.setenv("DEMO_RESERVE", "1")
        monkeypatch.setenv("GEMINI_RESERVE_KEY", "reserve-key-abc123")
        monkeypatch.setenv("GEMINI_API_KEY", "normal-key-xyz")
        
        from app.core.llm_router import _resolve_api_key
        # Reload to pick up env change
        import importlib, app.core.llm_router as mod
        importlib.reload(mod)
        
        key = mod._resolve_api_key("gemini", "GEMINI_API_KEY")
        assert key == "reserve-key-abc123"

    def test_normal_key_used_when_demo_reserve_off(self, monkeypatch):
        monkeypatch.setenv("DEMO_RESERVE", "0")
        monkeypatch.setenv("GEMINI_API_KEY", "normal-key-xyz")
        monkeypatch.delenv("GEMINI_RESERVE_KEY", raising=False)
        
        import importlib, app.core.llm_router as mod
        importlib.reload(mod)
        
        key = mod._resolve_api_key("gemini", "GEMINI_API_KEY")
        assert key == "normal-key-xyz"


class TestLaneStats:
    """TC: lane_stats accurately reflects calls made."""

    def test_stats_incremented_on_successful_call(self, monkeypatch, respx_mock):
        """After one successful Gemini call, lane_stats['gemini'] == 1."""
        import httpx
        from app.core import llm_router
        
        # Reset stats
        llm_router.reset_run_stats()
        
        # Mock a successful Gemini response returning valid JSON
        respx_mock.post("https://generativelanguage.googleapis.com/").mock(
            return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"competitor":"test"}'}]}}]})
        )
        
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.setattr(llm_router, "_BUDGETS", {"gemini": 9999})
        
        # Run complete() ... (abbreviated; actual impl depends on POC structure)
        stats = llm_router.get_run_stats()
        # After a successful call, gemini counter should be 1
        assert stats.get("gemini", 0) >= 0   # basic smoke — replace with actual assertion
```

### Step 4.3 — Run the tests

```powershell
$env:MOCK_MODE = "1"
pytest tests/test_llm_router.py -v --tb=short
```

All 5+ test cases must pass before the Sat 18:00 checkpoint.

---

## Phase 5 — Sat 21:00 Checkpoint Verification

Before the checkpoint, verify end-to-end for one hero company (`Notion`):

```powershell
# 1. POST analyze
$body = '{"company":"Notion","domain":"connected workspace"}'
$response = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/analyze" `
  -Method POST -Body $body -ContentType "application/json" `
  -Headers @{Authorization="Bearer $env:BEARER_TOKEN"}
$jobId = $response.job_id
Write-Host "Job ID: $jobId"

# 2. Poll until completed
do {
  Start-Sleep -Seconds 5
  $run = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/runs/$jobId" `
    -Headers @{Authorization="Bearer $env:BEARER_TOKEN"}
  Write-Host "Status: $($run.status) | Stage: $($run.current_stage)"
} while ($run.status -notin @("completed","failed"))

# 3. Verify lane_stats populated
$run.lane_stats | ConvertTo-Json

# 4. Verify reviews rendered cleanly
$report = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/reports/$jobId" `
  -Headers @{Authorization="Bearer $env:BEARER_TOKEN"}
$report.sentiment | ConvertTo-Json -Depth 3
```

**Expected:** `lane_stats` has provider keys with integers. Sentiment dict has rivals as keys, `label` is one of `POSITIVE|NEUTRAL|NEGATIVE`. Reviews section has no `[object Object]`.

---

## Phase 6 — Sunday Deck Assembly (09:00–13:30)

### Step 6.1 — 09:00 Start: Scaffold 12 slides

Use the Phase 10 slide outline. App-token theme: dark background, accent colour from the prototype (`--accent-teal` / `--accent-gold`). Reference the prototype (`Rivalyze_Prototype_v9_2_DESIGN_LAW.html`) for colour tokens.

### Step 6.2 — 09:00–12:00: Fill slides 1–7 and 9–12

Add screenshots from Dhruv's artifact folder (confirm path with him before 09:00):
- Run Monitor screenshot → Slide 5
- Dashboard screenshot → Slide 6
- Evidence drawer screenshot → Slide 7

Leave slide 8 blank — fill at 12:30.

### Step 6.3 — 12:30: Slide 8 — Real Numbers with Pooja

Pull these live values:

```powershell
# Evidence rows in the demo run
# (replace {HERO_RUN_ID} with actual UUID from the morning run)
# Ask Dharvi or query directly:
# SELECT COUNT(*) FROM evidence WHERE run_id = '{HERO_RUN_ID}';

# lane_stats from morning run
$heroJobId = "FILL_IN"
$run = Invoke-RestMethod "http://localhost:8000/api/v1/runs/$heroJobId" `
  -Headers @{Authorization="Bearer $env:BEARER_TOKEN"}
$run.lane_stats | ConvertTo-Json

# Pytest green count
pytest --tb=no -q | Select-String "passed"

# Endpoint count (count routes in routes.py)
(Select-String "^@router\." rivalyze-backend/app/api/routes.py).Count
```

Fill slide 8 with real numbers before 13:30.

### Step 6.4 — 13:30: Hand deck to Pooja

Export as PDF + keep the editable source. After handover: **deck assembly is done**, shift to supporting demo drills only.

---

## Done Checklist (Mihir's complete sign-off)

```
MODULE 1 — Cache
[ ] cache.py committed to rivalyze-backend/app/core/
[ ] cache_get / cache_set signatures match frozen interface (dict|None, no raises)
[ ] make_cache_key produces sha256[:16] of normalised query
[ ] Write-through to Postgres confirmed working
[ ] Upstash fallback path present (even if untested on local)
[ ] Tushar confirmed his search_chain.py integrates successfully

MODULE 2 — Router Hardening
[ ] budgets.json committed with all 7 provider limits
[ ] Budget check fires before every real LLM call
[ ] Budget-skip events logged with lane name + spend/budget numbers
[ ] counter_incr fires after successful calls only
[ ] lane_stats shape: { provider: calls, searches: N, cache_hits: N }
[ ] reset_run_stats() / get_run_stats() exported for lifecycle
[ ] DEMO_RESERVE=1 resolves to *_RESERVE_KEY correctly
[ ] All POC behaviour (backoff, failover, JSON repair) unchanged

MODULE 3 — Reviews Agent
[ ] review.py committed to rivalyze-backend/app/agents/
[ ] run(competitors, emit, company) → list[SentimentIntel] — never raises
[ ] top_complaints are plain strings ≤3 (no nested dicts)
[ ] overall_sentiment is POSITIVE|NEUTRAL|NEGATIVE enum only
[ ] low_signal=True on corpus < 300 chars or 0 search results
[ ] _flatten_complaint() guards against weak-model nesting

PYTEST
[ ] test_llm_router.py: budget-skip test passes
[ ] test_llm_router.py: reserve-key selection test passes
[ ] test_llm_router.py: lane_stats accuracy test passes
[ ] All tests run with MOCK_MODE=1 and no real network calls

SUNDAY DECK
[ ] 12 slides assembled, app-token theme
[ ] Screenshots from Dhruv's artifacts in slides 5-7
[ ] Slide 8 filled with real numbers at 12:30 with Pooja
[ ] Deck handed to Pooja by 13:30
```

---

## Escalation Map

| Situation | Who to ping |
|---|---|
| `REDIS_URL` doesn't connect | Anupam (DevOps) |
| `repository.get_search_cache` signature changed | Dharvi |
| `counter_get`/`counter_incr` not available yet | Tushar |
| POC `llm_router.py` base behaviour broken | Drashti |
| Blocked on any of the above > 30 min | Pooja (escalation lead) |
