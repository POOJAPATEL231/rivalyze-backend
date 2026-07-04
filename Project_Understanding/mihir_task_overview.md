# Mihir — Task File
> **Role:** Data Platform Backend Engineer · **Pod:** Data Platform (pair: Tushar)
> **Total estimated hours:** ~14h (Sat + Sun)
>
> **Purpose of this document:** Feed verbatim to an LLM or agentic coder to implement Mihir's three modules.
> This synthesises `mihir_task.md`, `mihir_prompt.txt`, `08_module_reference.md`, and `virat_prompt.txt`.

---

## Summary of Ownership

Mihir owns **three modules** and **one Sunday deliverable**:

| # | What | File | When |
|---|---|---|---|
| 1 | **Cache** — Redis + Postgres write-through | `app/core/cache.py` | Sat 11:00–13:00 |
| 2 | **LLM Router Hardening** — budgets, lane_stats, demo-reserve | `app/core/llm_router.py` | Sat 13:00–15:00 + 17:30–18:30 |
| 3 | **Reviews Agent** — complaint mining, sentiment | `app/agents/review.py` | Sat 15:00–17:30 |
| 4 | **Sunday Deck** — 12-slide presentation assembly | PowerPoint/Slides | Sun 09:00–13:30 |

**Pod relationship:** Tushar leads `search_chain.py` + `counters.py`. Mihir leads cache + router. They review each other's PRs before merge. The cache module (`cache_get`/`cache_set`) is **consumed by Tushar's search chain** — Mihir must publish it before Sat 13:00.

---

## MODULE 1 — Cache (`app/core/cache.py`)
**Time block:** Saturday 11:00–13:00 | **Test:** TC-P02

### What it does
A transparent two-layer cache. Callers (`search_chain.py`) call `cache_get`/`cache_set` without knowing the backend. The cache must **never** break a run — all failures degrade silently to a miss.

### Function signatures (FROZEN — Tushar's search chain depends on these)

```python
def cache_get(key: str) -> dict | None: ...
def cache_set(key: str, value: dict, ttl: int = 86400) -> None: ...
```

### Cache key format

```python
import hashlib
key = hashlib.sha256(query.lower().strip().encode()).hexdigest()[:16]
```

### Read path

```
cache_get(key)
    ├─ Try Redis GET  →  hit: json.loads + return
    ├─ Redis miss / error  →  emit one event ("cache · redis miss: {key}")
    ├─ Try repository.get_search_cache(key)  →  hit: return dict
    └─ Miss: return None
```

### Write path

```
cache_set(key, value, ttl)
    ├─ Redis SETEX key ttl json.dumps(value)
    └─ repository.save_search_cache(key, value)   ← write-through, survives Redis flush
```

### Redis connection

```python
import redis, os

_client: redis.Redis | None = None

def _get_client() -> redis.Redis | None:
    global _client
    if _client is None:
        url = os.getenv("REDIS_URL")
        if url:
            _client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_timeout=3
            )
    return _client
```

- **One module-level lazy singleton** — do not create a new connection per call.
- `REDIS_URL` format: `rediss://:KEY@NAME.redis.cache.windows.net:6380/0` (TLS, port 6380).
- `socket_timeout=3` — never block a run on cache.

### Upstash fallback

If `REDIS_URL` is absent but `UPSTASH_URL` + `UPSTASH_TOKEN` are set:

```python
# Use httpx to call Upstash REST API
# GET  {UPSTASH_URL}/get/{key}           Headers: Authorization: Bearer {UPSTASH_TOKEN}
# POST {UPSTASH_URL}/setex/{key}/{ttl}   Body: json.dumps(value)
# Same cache_get / cache_set signatures — callers must not know the difference
```

### Degradation rules

- Any exception (connection error, timeout, JSON decode error) → catch, emit **one** event, return `None` from `cache_get` / no-op `cache_set`.
- **Never raise** from these functions.
- **Never call `sys.exit`** or crash the process.

### Repository functions consumed (Dharvi's — do not redefine)

```python
repository.get_search_cache(key: str) -> dict | None
repository.save_search_cache(key: str, value: dict) -> None
```

### Test: TC-P02
- Simulate Redis flush mid-run → subsequent `cache_get` falls back to Postgres → returns correct value.
- Simulate Redis down (socket timeout) → `cache_get` returns `None`, no exception propagated.
- Normalised query (`"  Notion "` vs `"notion"`) → same key → single DB row.

---

## MODULE 2 — LLM Router Hardening (`app/core/llm_router.py`)
**Time block:** Saturday 13:00–15:00 (Part 1) + 17:30–18:30 (Part 2) | **Test:** TC-N02 + custom pytest

> **Base:** The POC `llm_router.py` already implements: lane ordering, `httpx` calls, JSON repair, backoff with `retry-after` (cap 8s), 2 retries per lane, failover on 429/timeout/schema-fail, keyless lane skip. **Do NOT change this base behaviour.**

### What to add

#### 2a. Budget enforcement (Part 1)

```python
import json, os

# Load once at module import
_budgets: dict[str, int] = {}
_budgets_path = os.path.join(os.path.dirname(__file__), "../../budgets.json")
with open(_budgets_path) as f:
    _budgets = json.load(f)

# Before each LLM attempt inside complete():
from app.core.counters import counter_get, counter_incr

spent = counter_get(f"llm:{provider_name}")   # e.g. "llm:gemini"
budget = _budgets.get(provider_name, 999999)
if spent >= budget:
    emit({"agent": "router", "msg": f"lane {provider_name} at budget ({spent}/{budget}) — skip"})
    continue  # skip this lane, try next

# After a REAL successful call:
counter_incr(f"llm:{provider_name}")
```

#### 2b. `lane_stats` accumulation (Part 1)

```python
# Thread-local or run-scoped accumulator — the lifecycle stores this on the run row
# Structure: { "gemini": 3, "groq_8b": 1, "searches": 9, "cache_hits": 5 }

# complete() should return (validated_model, lane_name)
# The run lifecycle calls set_lane_stats(run_id, lane_stats) after pipeline finishes
# Mihir exposes a way to read accumulated stats, e.g. get_lane_stats() -> dict
```

`cache_hits` and `searches` come from the cache module's counters — coordinate with Tushar on how they're surfaced (a simple module-level counter is fine; increment in `cache_get` on hit).

#### 2c. Demo-reserve switch (Part 2)

```python
import os

DEMO_RESERVE = os.getenv("DEMO_RESERVE", "0") == "1"

def _resolve_key(provider_name: str, default_env: str) -> str | None:
    """Return the appropriate API key: reserve key if DEMO_RESERVE=1, else normal key."""
    if DEMO_RESERVE:
        reserve = os.getenv(f"{provider_name.upper()}_RESERVE_KEY")
        if reserve:
            return reserve
    return os.getenv(default_env)
```

This is flipped **ONLY on demo day** — never commit `DEMO_RESERVE=1` to the repo.

### Pytest cases required

```python
# test_llm_router.py

# Test 1: budget-exceeded skip
def test_budget_skip(monkeypatch, httpx_mock):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    # set counter to >= budget for all providers → complete() should raise RuntimeError
    # (all lanes exhausted) — callers convert this to low_signal
    ...

# Test 2: reserve key selection
def test_demo_reserve_key(monkeypatch):
    monkeypatch.setenv("DEMO_RESERVE", "1")
    monkeypatch.setenv("GEMINI_RESERVE_KEY", "reserve-key-123")
    assert _resolve_key("gemini", "GEMINI_API_KEY") == "reserve-key-123"

# Test 3: lane_stats accuracy
def test_lane_stats(httpx_mock):
    # Mock successful Gemini call → verify lane_stats["gemini"] == 1 after complete()
    ...
```

All tests mock `httpx` — no real network calls in tests.

### Interface exposed to the lifecycle

```python
def complete(
    task_class: Literal["extract", "reason"],
    prompt: str,
    schema: type[BaseModel],
    emit: Callable
) -> tuple[BaseModel, str]:
    """Returns (validated_model_instance, winning_lane_name). Raises RuntimeError if all lanes exhausted."""
    ...
```

---

## MODULE 3 — Reviews Agent (`app/agents/review.py`)
**Time block:** Saturday 15:00–17:30 | **Test:** TC-B01, sentiment bar rendering

> **Spec source:** Agent 2 in `virat_prompt.txt` — Mihir owns this agent entirely.

### Output model

```python
from pydantic import BaseModel, Field
from typing import Literal

class SentimentIntel(BaseModel):
    competitor: str
    top_complaints: list[str] = Field(default_factory=list, max_length=3)
    # SHORT plain strings e.g. "feature overload" — NEVER nested dicts or objects
    opportunity_gaps: list[str] = Field(default_factory=list, max_length=3)
    # One exploitable gap per complaint, framed as an opportunity for our company
    overall_sentiment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"] = "NEUTRAL"
    sources: list[str] = Field(default_factory=list)
    # Only real URLs that appear in the search corpus
    low_signal: bool = False
```

### Function signature

```python
async def run(
    competitors: list[str],
    emit: Callable[[dict], None]
) -> list[SentimentIntel]:
    """Mine customer complaints and sentiment for each competitor.
    
    For each competitor:
    - Searches for complaint/review data
    - Calls the LLM to extract structured SentimentIntel
    - Degrades gracefully to low_signal=True on thin corpus or extraction failure
    - Never raises — caller always gets a typed result
    """
```

### Search queries (per competitor)

```python
from datetime import datetime
MONTH = datetime.now().strftime("%B %Y")   # e.g. "July 2026"

queries = [
    f"{competitor} customer complaints problems {MONTH}",
    f"{competitor} negative reviews reddit {MONTH}",
]
```

### Corpus construction

```python
corpus = ""
for result in search_results:
    corpus += f"SOURCE: {result['url']}\n{result['title']}\n{result['content']}\n\n"
corpus = corpus[:5000]   # cap at 5,000 chars
```

### LLM system prompt

```
SYSTEM: Mine customer complaints about {competitor} from the corpus (reviews, Reddit, forums).
top_complaints: ≤3 SHORT plain strings ("feature overload"), no objects.
opportunity_gaps: one exploitable gap per complaint, phrased for {company}.
overall_sentiment: exactly one of POSITIVE|NEUTRAL|NEGATIVE.
sources: only URLs that actually appear in the corpus above.
ONLY JSON: {"top_complaints":[],"opportunity_gaps":[],"overall_sentiment":"NEUTRAL","sources":[]}
```

### Low-signal degradation

```python
if len(corpus) < 300 or not results:
    emit({"agent": "reviews", "msg": f"reviews · low signal: {competitor}"})
    return SentimentIntel(competitor=competitor, low_signal=True)
```

### Anti-nesting enforcement

The LLM prompt must explicitly forbid nested objects:

```
top_complaints: PLAIN SHORT STRINGS ONLY.
WRONG:  {"top": {"issue": "slow", "severity": "high"}}
RIGHT:  "slow performance on large boards"
```

If the LLM returns nested dicts, extract the first string value and flatten — never crash.

### Full implementation pattern

```python
from app.core.search_chain import search
from app.core.llm_router import complete
from app.db.models import SentimentIntel

async def run(competitors: list[str], emit) -> list[SentimentIntel]:
    results_out: list[SentimentIntel] = []
    for c in competitors:
        try:
            # 1. Search
            raw = []
            for q in [f"{c} customer complaints problems {MONTH}",
                      f"{c} negative reviews reddit {MONTH}"]:
                raw.extend(await search(q, emit))
            
            # 2. Build corpus
            corpus = "\n\n".join(
                f"SOURCE: {r['url']}\n{r['title']}\n{r['content']}" for r in raw
            )[:5000]
            
            # 3. Low-signal guard
            if len(corpus) < 300:
                emit({"agent": "reviews", "msg": f"reviews · low signal: {c}"})
                results_out.append(SentimentIntel(competitor=c, low_signal=True))
                continue
            
            # 4. LLM extraction
            prompt = REVIEWS_SYSTEM_PROMPT.format(competitor=c, corpus=corpus)
            model, _ = complete("extract", prompt, SentimentIntel, emit)
            results_out.append(model)
        
        except Exception as exc:
            emit({"agent": "reviews", "msg": f"reviews · error for {c}: {exc}"})
            results_out.append(SentimentIntel(competitor=c, low_signal=True))
    
    return results_out
```

---

## MODULE 4 — Sunday Deck Assembly
**Time block:** Sunday 09:00–13:30

### Deck spec: 12 slides, Phase 10 outline, app-token theme

| Slide | Content | Source |
|---|---|---|
| 1 | Title: "Rivalyze — Team Argus" | Branding assets |
| 2 | Problem statement | Team notes |
| 3 | Solution overview + elevator pitch | Splash screen copy |
| 4 | 7-node pipeline diagram | Architecture diagram |
| 5 | Live demo screenshot — Run Monitor | Dhruv's artifact folder |
| 6 | Live demo screenshot — Dashboard | Dhruv's artifact folder |
| 7 | Evidence drawer + confidence formula | Dhruv's artifact folder |
| 8 | **REAL numbers** (fill at 12:30 with Pooja) | Live demo run |
| 9 | Tech stack + honesty rules | This doc |
| 10 | Judge-facing claims audit | Evidence rows |
| 11 | Team — pod assignments + workload | 05_workload_rebalance.md |
| 12 | Next steps / Thank you | — |

### Slide 8 — Real numbers to fill at 12:30

Pull these from the live morning demo run:
- Total endpoints exposed: count from `routes.py`
- Pytest tests green: `pytest --tb=no -q` output
- Evidence rows in demo run: `SELECT COUNT(*) FROM evidence WHERE run_id = '{hero_run_id}'`
- lane_stats from the morning run: `GET /api/v1/runs/{hero_job_id}` → `lane_stats` field
- Cache hit rate: Tushar provides from `counter_get` report (Sun 08:30)

### Handover
Deck must be in Pooja's hands by **13:30 Sunday**. After that: support demo drills only.

---

## Friday Night — Hero Research Notes (30–40 min)

For each of: **Notion**, **Zomato**, **Razorpay**

Collect and paste into a raw links doc (Slack / Notion page):

| What | How many |
|---|---|
| Rival company names | 4–5 |
| Pricing page URLs | 1 per rival |
| Recent news links (2026) | 3 per rival |
| Complaint threads (Reddit/G2) | 2 per rival |

These are Pooja's quality yardstick at the Sat 14:00 checkpoint — they validate that the real pipeline is finding the right signals.

---

## Done Criteria

Mihir's work is **done** when all of the following are true:

- [ ] **Cache:** Flushing Redis mid-demo loses nothing (Postgres write-through survives)
- [ ] **Cache:** `cache_get` never raises, always returns `dict | None`
- [ ] **Router:** A lane at or over budget is skipped with a logged event — user sees no error
- [ ] **Router:** `DEMO_RESERVE=1` switches to reserve keys — verified in test
- [ ] **Router:** `lane_stats` shape `{provider: calls, searches: N, cache_hits: N}` is accurate
- [ ] **Reviews:** Complaints render as clean short strings in the Dashboard sentiment bars (no `[object Object]`, no nesting)
- [ ] **Reviews:** `overall_sentiment` is always one of `POSITIVE|NEUTRAL|NEGATIVE` (never free text)
- [ ] **Pytest:** All three test cases (budget-skip, reserve-key, lane_stats) pass with httpx mocked
- [ ] **Deck:** 12 slides in Pooja's hands by Sun 13:30 with real slide-8 numbers

---

## Key Interfaces Consumed by Mihir

### From Tushar (`counters.py`)
```python
counter_incr(name: str) -> int   # name e.g. "llm:gemini", "credits:tavily:2026-07-05"
counter_get(name: str) -> int
```

### From Dharvi (`repository.py`)
```python
repository.get_search_cache(key: str) -> dict | None
repository.save_search_cache(key: str, value: dict) -> None
```

### Exposed by Mihir for Tushar (`cache.py`)
```python
cache_get(key: str) -> dict | None
cache_set(key: str, value: dict, ttl: int = 86400) -> None
```

### Exposed by Mihir for all agents (`llm_router.py`)
```python
complete(
    task_class: Literal["extract", "reason"],
    prompt: str,
    schema: type[BaseModel],
    emit: Callable[[dict], None]
) -> tuple[BaseModel, str]   # (validated_instance, lane_name)
```
