# Tushar's Task Document — Everything You Need To Know
## Role: Data Platform (Search + Counters) + Product Agent | Team Argus, CodeClash 2026

---

## Your Big Picture Role

You are one of the three strongest backend engineers on this team (the trio is Sheel, Mihir, and you). You are called the **DATA PLATFORM POD** together with Mihir — you two are equal partners. Your stuff is literally the foundation that every AI agent depends on to search the web. Without your search chain, nothing works.

You own **3 things**:
1. **Credit Counters** — track how many API calls each search provider has used
2. **Search Chain** — the core search function that all agents call to get web data
3. **Product Agent** — the AI agent that extracts pricing, features, and positioning for each competitor

You carry **14 hours** of estimated work over Saturday–Sunday. This is the same as Mihir and the same as Virat — you're in the heavy-lifting tier.

---

## Your Pair Partner: MIHIR

You and Mihir are equal partners in the Data Platform Pod. Here's how the split works:

| You Own | Mihir Owns |
|---|---|
| Search chain (search_chain.py) | Cache layer (cache.py) |
| Credit counters (counters.py) | LLM router hardening (llm_router.py) |
| Product agent (agents/product.py) | Reviews agent (agents/review.py) |

**You review each other's merges.** Every time Mihir merges something, you review it. Every time you merge something, he reviews it. This is mandatory — AI output is a draft, pairs guarantee correctness.

**Your code depends on his:** Your search chain CALLS his `cache_get` and `cache_set` functions. So Mihir's cache module needs to exist before your search chain is fully testable. Plan for this — mock his functions until he delivers.

**His code depends on yours:** His router budgets use your `counter_get` function to check if a provider has hit its daily limit. So your counters need to be done before his router hardening is complete.

---

## MODULE 1: Credit Counters
### File: `app/core/counters.py`
### When: Saturday — build this FIRST, it's the shortest module

### What It Does (Plain English)
Every time your search chain calls Tavily or Serper (paid search APIs), you need to track how many calls were made that day. This is like a scoreboard. If Tavily's daily budget is used up, the router knows to skip it and go straight to Serper.

The counters are stored in Redis (the fast in-memory cache). The key looks like: `"credits:tavily:2026-07-04"` — so every day resets automatically (different date = different key).

### What You Write

```python
# app/core/counters.py

def counter_incr(name: str) -> int:
    # Adds 1 to the counter named 'name' in Redis
    # Returns the new total count
    # If Redis is down: return 0 silently, emit ONE event, NEVER crash

def counter_get(name: str) -> int:
    # Returns current value of counter 'name' from Redis
    # If Redis is down: return 0 silently, NEVER crash
```

### Key Names You'll Use
- `"credits:tavily:2026-07-04"` — Tavily calls today
- `"credits:serper:2026-07-04"` — Serper calls today
- `"llm:groq:2026-07-04"` — Groq LLM calls today (Mihir's router uses these)

### The Single Most Important Rule
**Counters must NEVER break a run.** If Redis is down, counter_incr returns 0 and emits one event — that's it. The search still happens. A failed counter is a silent log entry, not an error.

### What "Done" Looks Like
- `counter_incr("credits:tavily:2026-07-04")` returns 1, 2, 3... as you call it
- `counter_get` returns the same number
- Redis going down → you get 0 back, one event emitted, search continues normally

### Who Uses Your Counters
- **You:** call `counter_incr` every time search_chain calls Tavily or Serper
- **Mihir:** calls `counter_get` in the LLM router to check if a provider hit its daily budget
- **Dharvi:** `scripts/warmup.py` uses `counter_get` to print how many credits were used

---

## MODULE 2: Search Chain
### File: `app/core/search_chain.py`
### When: Saturday — this is your MAIN work, build after counters
### This is the most critical module — every AI agent depends on it

### What It Does (Plain English)
Think of this as a "web search function with backup plans." When any agent (News, Product, Reviews, Discovery) needs to search the internet, they all call YOUR `search()` function. You try 3 sources in order:

1. **Cache first** — if this exact query was searched before, return the saved result instantly (zero API calls)
2. **Tavily** — a paid search API, good quality results
3. **Serper** — another paid search API, fallback if Tavily fails
4. **Direct website scraping** — ONLY if the query is about a specific competitor's own domain (e.g., "notion.so pricing")

### The Exact Function Signature (FROZEN — don't change this)
```python
def search(query: str, emit) -> list[dict]:
    # Returns: [{"title": "...", "url": "https://...", "content": "..."}]
    # emit is a function you call to log progress events
```

This signature is frozen because all 5 agents will be importing and calling `search(query, emit)`. If you change the parameters, everything breaks.

### Step-by-Step Logic Inside search()

**Step 1: Normalize the query**
```
lowercase, strip whitespace → compute sha256 hash (first 16 chars) = cache key
```

**Step 2: Check the cache (Mihir's cache_get)**
```python
cached = cache_get(cache_key)
if cached:
    return cached  # Done! Zero external calls
```

**Step 3: Try Tavily**
- POST to `api.tavily.com/search` with `max_results=3`
- Use the `TAVILY_API_KEY` environment variable (if not set, skip Tavily entirely)
- Call `counter_incr("credits:tavily:YYYY-MM-DD")` when you make the call
- If it works: go to "cache and return"
- If it fails: move to Serper

**Step 4: Try Serper**
- POST to `google.serper.dev/search`
- Use header `X-API-KEY: {SERPER_API_KEY}` (if not set, skip)
- Map the response's `organic[]` array to `[{title, url, content}]` shape
- Call `counter_incr("credits:serper:YYYY-MM-DD")`
- If it works: go to "cache and return"
- If it fails: move to direct scrape

**Step 5: Direct scrape (ONLY for competitor domain queries)**
- This only runs if the query string contains a known competitor's domain (e.g., "notion.so" or "clickup.com")
- **First**, fetch and check `/robots.txt` using Python's `urllib.robotparser`
  - If the page is disallowed: emit an event and SKIP (do not scrape)
- **Then**, do an httpx GET with `User-Agent: "RivalyzeBot/0.1 (+hackathon demo)"`
- Use BeautifulSoup to extract text (strip `<nav>`, `<script>`, `<style>` tags)
- Only take the first 2,000 characters of the main text
- **Never follow links off the domain** (stay on the exact domain you were given)
- 8-second timeout

**Step 6: Cache and return**
```python
if results:  # only cache non-empty results
    cache_set(cache_key, results)
    return results
else:
    emit("search", "no results found for query")
    return []  # empty is valid — callers handle it as "low signal"
```

### The "NO ddgs" Rule
DuckDuckGo (ddgs) is explicitly banned. Do not use it, do not import it. Tavily → Serper → scrape. That's the chain.

### What "Done" Looks Like (The 3 "Done" Criteria)
1. **Repeat query = 0 external calls** — search the same thing twice, second time hits cache and never calls Tavily or Serper
2. **Tavily revoked mid-run → Serper takes over on screen** — QA (Rushabh) will revoke the Tavily key live during Sunday's TC-N01 test and watch the events show Serper being used
3. **Product tiers render un-nested on Krutarth's h2h** — meaning your search results feed the Product agent correctly

### Tests You Must Write (5 pytest cases)
Use `respx` or `monkeypatch` to mock httpx — never make real API calls in tests:
1. Cache hit → returns cached result, no HTTP calls made
2. Tavily succeeds → result cached, counter incremented
3. Tavily fails → Serper called, result cached
4. Both APIs fail, no competitor domain → returns []
5. Query contains competitor domain → robots.txt checked, content scraped

---

## MODULE 3: Product Agent
### File: `app/agents/product.py`
### When: Saturday 17:30–20:00 (after search chain is done)

### What It Does (Plain English)
This is an AI agent that takes a list of competitors and, for each one, searches the web to find:
- What their pricing plans are (and how much they cost)
- What new features they recently launched
- How they position themselves in the market
- What advantages YOUR company has against them

The output feeds directly into the head-to-head table on the dashboard that Krutarth builds.

### How To Build It

**Clone Virat's news agent** (`app/agents/news.py`) as your skeleton — same structure, different search queries and different LLM prompt.

### The Function Signature
```python
def run(competitors: list, emit) -> list[dict]:
    # For each competitor, search + extract product intel
    # Returns list of ProductIntel objects
```

### For Each Competitor, You Do:
1. Run 2–3 search queries through YOUR search chain:
   - `"{competitor} pricing plans July 2026"`
   - `"{competitor} vs {company} comparison July 2026"` ← comparison articles are goldmines
   - `"{competitor} new features 2026"`
2. Combine all search results into a corpus (cap at ~5,000 chars)
3. Call `complete("extract", prompt, schema, emit)` — Mihir's LLM router handles which AI model to use
4. The AI extracts the structured data

### The Output Shape (Per Competitor)
```json
{
  "competitor": "Coda",
  "pricing_tiers": ["Pro $12/seat: AI included", "Enterprise: custom from 100 seats"],
  "recent_features": ["Enterprise sync GA", "AI formulas v2"],
  "positioning": "docs-as-apps for power teams",
  "advantages": ["simpler onboarding story than Notion", "cheaper at small team size"],
  "sources": ["https://coda.io/pricing", "https://techcrunch.com/..."],
  "low_signal": false
}
```

### The #1 Rule for Pricing Tiers: PLAIN STRINGS ONLY
```
CORRECT:   "Pro $12/seat: AI included"
WRONG:     {"tier": "Pro", "price": "$12", "features": ["AI"]}
```

LLMs naturally output nested objects for pricing. Your LLM prompt MUST explicitly say:
> "pricing_tiers are PLAIN STRINGS like 'Pro $12/seat: AI included' — NEVER nested objects"

And include a wrong example in the prompt so the model knows exactly what NOT to do.

### The LLM System Prompt for Your Agent
```
SYSTEM: Extract pricing and product positioning for {competitor}. 
pricing_tiers are PLAIN STRINGS like "Pro $12/seat: AI included" — 
NEVER nested objects (wrong: {"tier":"Pro",...}).
advantages = angles {company} can use AGAINST them, from the corpus only. 
Every source in "sources" must be a URL from the corpus. 
ONLY JSON: {"pricing_tiers":[],"recent_features":[],"positioning":"","advantages":[],"sources":[]}
```

### When Corpus Is Too Thin (Low Signal)
If search returns very little (< 300 chars) or the LLM can't extract anything useful:
```json
{
  "competitor": "Slite",
  "pricing_tiers": [],
  "recent_features": [],
  "positioning": "",
  "advantages": [],
  "sources": [],
  "low_signal": true
}
```
Never raise an exception. Always return a valid object, even if it's mostly empty.

### Who Consumes Your Product Agent Output
- **Gati's merge node** — takes your output and turns it into evidence rows in the database
- **Krutarth's Dashboard** — the head-to-head table shows pricing_tiers from your agent
- **Sheel's Strategist** — uses your data (with evidence IDs from Gati) to write strategy

---

## Sunday Tasks (Your Data Engineering Day)

### 08:30 — Cache/Credit Report to the Group
Run this every morning after the first real run:
```python
# Call counter_get for each provider
counter_get("credits:tavily:2026-07-04")   # Tavily calls used
counter_get("credits:serper:2026-07-04")  # Serper calls used
counter_get("llm:groq:2026-07-04")        # Groq calls used
# Also: check lane_stats of last 10 runs for cache hit rate
```
Post these numbers in the group chat. This is how the team knows if they're running low on credits before the demo.

### 10:30 — TC-N01 Drill with Rushabh
This is a LIVE test — you and Rushabh (QA) will:
1. Start a fresh analysis run
2. Mid-run, Rushabh REVOKES the Tavily API key
3. Watch the events on screen show Serper taking over
4. Verify the run still completes successfully

This is one of the judge-impressive moments — "our system is resilient even if a paid API goes down mid-run."

### 11:00–13:00 — Evidence URL Integrity Check with Dharvi
Go through every evidence row from the overnight hero runs and verify every URL is actually live:
```python
import httpx
# HEAD request each URL (faster than GET — just checks if it exists)
response = httpx.head(url, timeout=5)
# Report any 404s or dead links to Drashti
```
The judges will click on these URLs. A 404 during the demo is embarrassing. Check them all.

### 13:30 — Counters on the Wall for Demo
Darshit will post the real-time counter values on a screen during the demo. You are the one who explains what the numbers mean to the judges when they ask.

---

## Your "Done" Definition (How You Know You're Finished)

| Check | What To Do |
|---|---|
| Repeat query = 0 external calls | Search the same query twice — second time should emit "cache hit" |
| Tavily revoked mid-run → Serper visible on screen | TC-N01 with Rushabh |
| Product tiers render un-nested | Krutarth's h2h table shows "Pro $12/seat: AI included", NOT an object |

---

## Full Collaboration Map (Who You Talk To And When)

| Person | Why You Interact | When |
|---|---|---|
| **Mihir** (pair partner) | You import his `cache_get`/`cache_set` · He uses your `counter_get` · Review each other's merges | All day Saturday |
| **Dharvi** | You call her `repository.save_evidence` (via Gati) · Sunday: URL integrity check together | Saturday evening + Sunday 11:00 |
| **Gati** | Her merge node consumes your Product agent output and turns it into EvidenceRows | Saturday evening |
| **Krutarth** | His Dashboard h2h table shows your `pricing_tiers` — must be plain strings or his UI breaks | Saturday night |
| **Rushabh** (QA) | He runs TC-N01 (search fallback drill) with you live on Sunday 10:30 | Sunday 10:30 |
| **Drashti** | She reviews anything that touches the API contract or pipeline wiring | Saturday (if issues) |
| **Sheel** | His Product agent spec (PART 1 of Sheel's prompt) is the spec for your Module 3 | Saturday 17:30 |

---

## How To Use The AI Prompt File (tushar_prompt.txt)

Your `tushar_prompt.txt` is a ready-made starting prompt for GitHub Copilot or Google Antigravity. Here's the flow:

1. **Saturday 10:00** — Clone the POC repo
2. **Open your prompt file** — paste it WHOLE into Copilot Chat or Antigravity as the first message
3. **Get the AI to generate** `counters.py`, `search_chain.py`, and eventually `product.py`
4. **Review every line with Mihir** — AI output is a draft, not production code
5. **Run your 5 tests** — if they pass, you're good to merge

---

## Important Environment Variables You Need

```
REDIS_URL=rediss://....:6380    # Azure Cache for Redis (TLS, note the rediss:// with 2 s's)
TAVILY_API_KEY=tvly-...         # From Darshit's vault
SERPER_API_KEY=...              # From Darshit's vault
```

If `TAVILY_API_KEY` is missing → skip Tavily entirely (no error, just skip)
If `SERPER_API_KEY` is missing → skip Serper entirely (no error, just skip)
If `REDIS_URL` is missing → counters silently fail, cache always misses

---

## Things That Will Trip You Up (And How To Avoid Them)

### 1. Robots.txt must be checked BEFORE scraping
Do not skip this. If the page says "Disallow: /pricing", you skip that URL and emit an event. The `urllib.robotparser` module does this cleanly.

### 2. The `emit` parameter is a function, not a string
```python
def search(query: str, emit) -> list[dict]:
    emit("search", "calling Tavily...")  # ← this is how you log
```
Every meaningful step should emit an event — this is what shows on the run monitor screen.

### 3. Cache key normalization matters
`"Notion pricing"` and `"notion pricing"` and `" notion pricing "` must produce the SAME cache key. Normalize before hashing: `query.lower().strip()`.

### 4. Pricing tiers MUST be plain strings
If you let the LLM output nested objects and Krutarth's UI receives `[{"tier": "Pro", "price": 12}]` instead of `["Pro $12/seat: AI included"]`, his table will break. This is the #1 QA check for your module (TC-B01-style nesting check).

### 5. Never follow off-domain links when scraping
If you're scraping `notion.so/pricing`, never follow a link to `twitter.com` or `youtube.com`. Strict domain boundary.

### 6. Empty results are valid
If search returns `[]`, that is fine. The Product agent will set `low_signal: true`. Never raise an exception. Callers handle empty gracefully.

---

## File Locations Summary

| Module | File Path | Status |
|---|---|---|
| Credit Counters | `app/core/counters.py` | You write from scratch |
| Search Chain | `app/core/search_chain.py` | You write from scratch |
| Product Agent | `app/agents/product.py` | Clone from news.py skeleton |
| Your Tests | `tests/test_search_chain.py` | 5 tests, httpx mocked |
| Cache (import from) | `app/core/cache.py` | Mihir writes this |
| LLM Router (call via) | `app/core/llm_router.py` | Mihir writes this (base = POC) |
