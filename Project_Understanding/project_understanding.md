# Rivalyze — Complete Project Understanding
> **Team:** Argus · **Event:** CodeClash 2026 · **Repos:** `rivalyze-backend` + `rivalyze-frontend`
>
> **Purpose of this document:** Feed verbatim to any LLM or agentic coding tool as complete project context.
> If this document conflicts with an individual prompt file, **the prompt file wins**.

---

## 1. Product Vision

**Rivalyze** is a multi-agent competitive intelligence platform. A user inputs a company name (or a startup idea) and the system:

1. **Discovers** up to 4 real rivals automatically
2. **Gathers signals** in parallel — recent news, product/pricing data, customer reviews
3. **Merges** all findings into typed `EvidenceRow` objects with claim references
4. **Synthesises** a board-ready `CompetitiveReport` with code-computed confidence scores
5. **Exposes** every claim's evidence via an in-app drawer (full audit trail)

### Elevator Pitch (memorise this — it appears verbatim on the Splash screen)
> "Rivalyze turns a company name into a board-ready competitive report in minutes, with every claim evidence-linked and every confidence score computed — not guessed."

---

## 2. The 7-Node Pipeline

```
User Input (company / idea)
        │
        ▼
┌───────────────────┐
│  [1] Discovery    │  → CompetitorSet (≤4 rivals); FIRST real e2e by Sat 13:00
└────────┬──────────┘
         │  parallel fan-out (LangGraph additive reducers)
   ┌─────┴──────┬──────────────┐
   ▼            ▼              ▼
[2] News    [3] Product   [4] Reviews   ← PARALLEL (Virat / Tushar / Mihir)
   │            │              │
   └─────┬──────┴──────────────┘
         ▼
┌───────────────────┐
│   [5] Merge       │  → EvidenceRows persisted; UnifiedSignals assembled; confidence computed
└────────┬──────────┘
         ▼
┌───────────────────┐
│  [6] Strategist   │  → CompetitiveReport (model draft); code overrides confidence + drops bad cites
└────────┬──────────┘
         ▼
┌───────────────────┐
│   [7] Validate    │  → schema sanity + ONE cross-lane repair retry; never raises to caller
└────────┬──────────┘
         ▼
  CompetitiveReport stored in PostgreSQL → served via GET /api/v1/reports/{run_id}
```

**LangGraph rule:** parallel branches (nodes 2-4) MUST write only to fields declared `Annotated[list[X], operator.add]`. Plain field assignment in parallel nodes causes clobbering — this is a hard architectural rule.

---

## 3. Technology Stack

| Layer | Primary | Fallback / Notes |
|---|---|---|
| **Language** | Python 3.12 | 3.11 or 3.13 = fix it |
| **API Framework** | FastAPI + Pydantic v2 | — |
| **Graph Engine** | LangGraph (StateGraph) | — |
| **HTTP Client** | httpx | — |
| **Database** | Azure PostgreSQL Flexible (psycopg pool) | Supabase connection string (env `SUPABASE_DATABASE_URL`) |
| **Cache** | Azure Cache for Redis — `redis-py`, TLS port 6380 | Upstash REST via httpx (env `UPSTASH_URL`+`UPSTASH_TOKEN`) |
| **Backend Host** | Azure App Service | — |
| **Frontend** | Vite + React + TypeScript + Tailwind | — |
| **Frontend Host** | Azure Static Web Apps | Vercel (one env-flip fallback) |
| **LLM (extract lane order)** | Groq-8b → Gemini → Cerebras → OpenRouter-Llama | — |
| **LLM (reason lane order)** | Gemini → Cerebras → Groq-70b → OpenRouter-DeepSeek | — |
| **Search order** | Tavily (max 3) → Serper → own-domain scrape | No `ddgs` — ever |
| **Design Law** | `Rivalyze_Prototype_v9_2_DESIGN_LAW.html` | Pixels, copy, and states are final |

---

## 4. Frozen API Contract
> **Contract freezes Saturday 11:00. Zero changes after that.**

```
POST /api/v1/analyze
  Body:   { company: str, domain?: str, idea?: str }
  Return: { job_id: str }
  Rule:   If find_completed_report(company) hits → return existing job_id + status=completed instantly.
          Never run the pipeline synchronously. Use FastAPI BackgroundTasks.

GET  /api/v1/runs/{job_id}
  Return: { status, current_stage, events[{t, agent, msg}], partial, lane_stats }
  Poll:   Frontend polls every 2,000ms; dedupes by event index.

GET  /api/v1/reports/{run_id}    → CompetitiveReport (full JSON)
GET  /api/v1/evidence/{claim_ref}?run_id=  → { claim_ref, sources: [EvidenceRow] }
GET  /api/v1/history             → [{ job_id, company, threat_level, confidence, created_at }]
                                   newest-first; ?company= filter (ILIKE)
GET  /api/v1/health              → { status: "ok" }   (no auth required)

# Stretch — build ONLY after Sat 21:00 gate:
POST /api/v1/documents
POST /api/v1/chat
GET  /api/v1/reports/{run_id}/export?format=md
```

**Auth:** `Authorization: Bearer {BEARER_TOKEN}` on all routes except `/health`.

**Error contract:** Unknown `job_id` → `404 { detail: "..." }`. No stack traces. Pipeline exceptions caught in background task → `status=failed` with events; the polling client never sees a 500.

---

## 5. Data Models (`app/db/models.py`)

```python
# --- Discovery ---
class Competitor(BaseModel):
    name: str
    category: Literal["direct", "indirect"] = "direct"
    rationale: str = ""

class CompetitorSet(BaseModel):
    competitors: list[Competitor] = Field(default_factory=list, max_length=4)

# --- News ---
class NewsItem(BaseModel):
    event: str; impact: str
    source_url: str   # MUST be a real URL from the search corpus — not a publication name
    date: str = ""

class NewsSignals(BaseModel):
    competitor: str
    items: list[NewsItem] = Field(default_factory=list, max_length=4)
    low_signal: bool = False

# --- Product ---
class ProductIntel(BaseModel):
    competitor: str
    pricing_tiers: list[str]   # PLAIN STRINGS e.g. "Pro $12/seat: AI included" — NEVER nested dicts
    recent_features: list[str]
    positioning: str = ""
    advantages: list[str]      # framed FOR our company (opportunities against the rival)
    sources: list[str]         # real corpus URLs only
    low_signal: bool = False

# --- Reviews (Mihir owns this output type) ---
class SentimentIntel(BaseModel):
    competitor: str
    top_complaints: list[str] = Field(default_factory=list, max_length=3)   # SHORT plain strings
    opportunity_gaps: list[str] = Field(default_factory=list, max_length=3) # one per complaint
    overall_sentiment: Literal["POSITIVE", "NEUTRAL", "NEGATIVE"] = "NEUTRAL"
    sources: list[str]
    low_signal: bool = False

# --- Evidence (Gati's merge output — stored in DB) ---
class EvidenceRow(BaseModel):
    id: str            # "ev-" + uuid4().hex[:8]
    run_id: str
    claim_ref: str     # e.g. "pricing:coda" or "rec:bundle-ai"
    source_type: Literal["news", "pricing", "review", "web", "document"]
    source_name: str
    url: str
    snippet: str       # ≤280 chars (enforced by Field(max_length=280))
    source_date: str = ""
    agent: str

class Signal(BaseModel):
    run_id: str; agent: str; competitor: str
    type: Literal["launch", "funding", "pricing", "feature", "complaint", "sentiment"]
    payload: dict
    evidence_ids: list[str] = Field(default_factory=list)

class UnifiedSignals(BaseModel):
    signals: list[Signal] = Field(default_factory=list)
    per_competitor: dict = Field(default_factory=dict)   # rollups including evidence_ids
    low_signal_findings: list[str] = Field(default_factory=list)

# --- Final Report ---
class Recommendation(BaseModel):
    action: str; rationale: str
    confidence: float = Field(ge=0.05, le=0.95)   # ALWAYS code-computed, never from model
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ref: str

class Opportunity(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ref: str

class CompetitiveReport(BaseModel):
    company: str
    threat_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    executive_summary: str
    swot: Swot                           # {strengths, weaknesses, opportunities, threats}
    sentiment: dict[str, SentimentScore]
    head_to_head: list[H2HRow]
    opportunities: list[Opportunity]
    recommendations: list[Recommendation] = Field(max_length=3)
    low_signal_findings: list[str]
    analysis_date: str
```

---

## 6. PostgreSQL Schema (Owner: Dharvi)

| Table | Key columns | Purpose |
|---|---|---|
| `companies` | `id`, `slug` (unique), `name`, `is_hero` | Canonical company registry |
| `runs` | `job_id` (unique), `status`, `current_stage`, `events` (jsonb), `lane_stats` (jsonb) | One row per pipeline execution |
| `reports` | `run_id` (unique), `report` (jsonb), `md_export` | CompetitiveReport blob |
| `competitors` | `run_id`, `name`, `category` | Rivals found per run |
| `signals` | `run_id`, `agent`, `type`, `payload` (jsonb), `evidence_ids` (jsonb) | Per-agent typed findings |
| `evidence` | `id` (`ev-*`), `run_id`, `claim_ref`, `source_type`, `url`, `snippet ≤280` | **The audit trail** |
| `search_cache` | `key` = `sha256(norm_query)[:16]`, `value` (jsonb) | Query memoisation |
| `kb_stores` | `company_slug`, `store_name`, `is_hero` | RAG store bookkeeping |
| `documents` | `company_slug`, `filename`, `chunks` | Uploaded docs |

**Key indexes:** `runs(job_id)`, `evidence(run_id, claim_ref)`, `companies(lower(name))`

---

## 7. Module Ownership Map

### Backend

| # | Module | File Path | Owner | Pair |
|---|---|---|---|---|
| B1 | API routes + lifecycle | `app/api/routes.py`, `app/main.py` | Drashti | — |
| B2 | LangGraph orchestrator | `app/core/orchestrator.py` | Virat | Drashti (reviews) |
| B3 | Discovery agent | `app/agents/discovery.py` | Sheel | — |
| B4 | News agent | `app/agents/news.py` | Virat | — |
| **B5** | **Reviews agent** | **`app/agents/review.py`** | **Mihir** | — |
| B6 | Product agent | `app/agents/product.py` | Tushar | — |
| B7 | Strategist agent | `app/agents/strategist.py` | Sheel | Virat (pairs) |
| B8 | Merge + confidence | `app/core/merge.py`, `app/core/confidence.py` | Gati | Drashti |
| **B9** | **LLM router (hardened)** | **`app/core/llm_router.py`** | **Mihir** (base = POC) | Tushar |
| B10 | Search chain + counters | `app/core/search_chain.py`, `app/core/counters.py` | Tushar | Mihir |
| **B11** | **Cache** | **`app/core/cache.py`** | **Mihir** | Tushar |
| B12 | Knowledge/RAG | `app/core/kb.py` | Sheel | — |
| B13 | Database + repository | `app/db/schema.sql`, `app/db/repository.py` | Dharvi | — |
| B14 | History endpoint + Markdown export | `GET /history`, `app/core/export.py` | Dharvi | — |
| B15 | Warm-up script | `scripts/warmup.py` | Dharvi | — |

### Frontend

| Module | Owner | Published by |
|---|---|---|
| Foundation (tokens, client, polling, Demo Mode, router, splash) | Dhwani | Sat 13:00 |
| Brief + Discovery views | Dhwani | — |
| Run Monitor view | Akash | — |
| History view + `DegradedCard`/`LowSignalBanner` | Akash | — |
| Dashboard view | Krutarth | — |
| Evidence components + Recommendations | Vatsal | Sat 18:00 |
| Compare / Workspace (stretch) | Krutarth / Vatsal | After Sat 21:00 gate |

---

## 8. Confidence Formula

```
confidence(source_count, agreeing, corroborating_agents) =
    clamp(
        0.25
        + 0.12 × min(source_count, 5)
        + 0.15 × (agreeing / max(source_count, 1))
        + 0.10 × min(corroborating_agents, 3),
        0.05,  # minimum
        0.95   # maximum
    )
```

- `confidence(1, 1, 1)` **must** be visibly below 0.5 — there is no artificial floor.
- Model-asserted confidence numbers are **always discarded**. The code recomputes every value.
- Recommendations citing evidence IDs not present in input are **silently dropped** with a warning event.

---

## 9. LLM Router Lane Order (`app/core/llm_router.py`)

| Task class | Lane 1 | Lane 2 | Lane 3 | Lane 4 |
|---|---|---|---|---|
| `"extract"` | Groq Llama-3.1-8B | Gemini 2.5 Flash | Cerebras Llama-3.3-70B | OpenRouter-Llama |
| `"reason"` | Gemini 2.5 Flash | Cerebras Llama-3.3-70B | Groq Llama-3.3-70B | OpenRouter-DeepSeek |

**Failover triggers:** 429 → backoff honoring `retry-after` (cap 8s) → retry ×2 → next lane. Schema fail → one JSON-repair attempt (strip fences, extract outermost object) → next lane. Keyless lanes are skipped silently.

---

## 10. Environment Variables

```bash
# LLM API keys
GEMINI_API_KEY=
GROQ_API_KEY=
CEREBRAS_API_KEY=
OPENROUTER_API_KEY=

# Search API keys
TAVILY_API_KEY=
SERPER_API_KEY=

# Database
DATABASE_URL=postgresql://rivalyze:PW@SERVER.postgres.database.azure.com:5432/rivalyze?sslmode=require

# Cache (primary)
REDIS_URL=rediss://:KEY@NAME.redis.cache.windows.net:6380/0

# Auth
BEARER_TOKEN=change-me
FRONTEND_ORIGIN=http://localhost:5173

# Mode switches
MOCK_MODE=0          # 1 = return 4 mock rivals, no real API calls
DEMO_RESERVE=0       # 1 = prefer *_RESERVE_KEY env vars (flipped ONLY on demo day)

# Inactive fallbacks (only read if primary is absent)
SUPABASE_DATABASE_URL=
UPSTASH_URL=
UPSTASH_TOKEN=
BEDROCK_API_KEY=

# Reserve keys (read ONLY when DEMO_RESERVE=1)
GEMINI_RESERVE_KEY=
GROQ_RESERVE_KEY=
CEREBRAS_RESERVE_KEY=
OPENROUTER_RESERVE_KEY=
```

---

## 11. Budget Config — `backend/budgets.json` (owner: Mihir)

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

Budget enforcement: before each real call → `counter_get(provider)` → if `spent >= budget` → skip lane, emit event (same treatment as a missing key). Increment `counter_incr(provider)` only on successful real calls.

---

## 12. Honesty Rules (judges will probe these directly)

| Rule | Detail |
|---|---|
| **No uncomputed visuals** | Every number, badge, bar, and chip derives from evidence. Violating this is the "random-threat-matrix mistake". |
| **Truthful stat strip** | Shows: `5 agents · 4 LLM lanes · 100% claims evidence-linked`. NOT "1200+ sources" (fabricated, rejected). |
| **Agent count = 5** | Discovery, News, Product, Reviews, Strategist. Merge and Validate are deterministic code nodes — never "agents". |
| **Full confidence range** | Drawer footer always shows: `"confidence X = f(N sources, agreement, M agents) · full 0–1 range, never floored"` |
| **Low signal = typed finding** | `low_signal: true` writes to `low_signal_findings[]`, never surfaces as an error. |
| **Source chips = actual sources** | No LinkedIn (not sourced). No GitHub (no agent). Chips reflect real pipeline outputs only. |
| **Chat refuses off-source** | Exact refusal text: `"not found in the report or your uploaded documents"` |

---

## 13. Timeline

| Window | What happens |
|---|---|
| **Friday (tonight)** | Accounts/keys/Azure deploy proofs · POC mock run verified on all machines · Go/no-go 8 PM · Hero research notes (Mihir: Notion/Zomato/Razorpay links) |
| **Sat 10:00** | Clone POC, scaffold repos |
| **Sat 11:00** | ⛔ **CONTRACT FREEZE** |
| **Sat 11:00–13:00** | Cache module (Mihir) |
| **Sat 13:00** | Router + DB live · Discovery first real e2e on Azure |
| **Sat 13:00–15:00** | Router hardening Part 1 (Mihir) |
| **Sat 14:00** | Frontend foundation + Demo Mode fixtures published (Dhwani) |
| **Sat 15:00–17:30** | Reviews agent (Mihir) |
| **Sat 17:30–18:30** | Router hardening Part 2 + pytest (Mihir) |
| **Sat 18:00** | First full agent e2e |
| **Sat 21:00** | ⛔ **CHECKPOINT** — one hero company end-to-end → stretch gate opens |
| **Sat night** | Warm-up ~15 hero companies |
| **Sun 09:00** | Frontend connects to real API · Mihir starts deck assembly |
| **Sun 12:00** | Rehearsal #1 |
| **Sun 12:30** | Mihir + Pooja: hero quality pass + fill slide-8 with real numbers |
| **Sun 13:30** | ⛔ **FEATURE FREEZE** · Mihir hands deck to Pooja |
| **Sun ~13:30** | Demo |

---

## 14. Hero Companies (warm-up targets — Sat night)

`Notion, Zomato, Razorpay, Swiggy, Zerodha, Paytm, PhonePe, Figma, Canva, Slack, Linear, Airtable, Duolingo, Ola, Zepto`

---

## 15. Test Coverage Reference (for QA & devs)

| Module | Test IDs | What QA asserts |
|---|---|---|
| routes + lifecycle | TC-C01–C08 | Contract shapes, 401/404, instant re-run (C05) |
| orchestrator | TC-N03, TC-C04 | Killed agent → run still completes; events stream |
| discovery | TC-U03, Tier-4 "Coda" | 4 rivals proposed; never includes input company |
| agents (all three) | TC-B01, TC-B05, TC-N04 | Malformed output → low_signal not crash; no fake URLs |
| strategist | TC-B04, TC-U05/U07 | Unknown evidence_ids dropped; ≤3 recs; sub-0.5 renders rose ring |
| merge + confidence | TC-B02/B03, TC-U06 | `confidence(1,1,1) < 0.5`; formula in drawer footer |
| **llm_router** | **TC-N02** | **429 storm → failover events, no user error; lane_stats accurate** |
| **cache + search** | **TC-N01, TC-P02** | **Provider fallback; Redis flush harmless; normalised query = cache hit** |
| repository | TC-P01, Tier-4 SQL-string | Backend restart loses nothing; parameterised queries only |
| history + export | TC-C07, TC-U08 | Newest-first; clean `.md` renders in VS Code preview |

---

## 16. Global Rules (everyone)

1. **No new dependencies** without Drashti + Anupam approval.
2. **Blocked > 30 min** → escalate: pair → pod lead → Pooja.
3. **Every judge-visible claim** must trace to an `EvidenceRow`.
4. **Contract freeze is absolute** — Sat 11:00, no exceptions.
5. **Docstrings are mandatory** in every module — they are the documentation.
6. **If you change an interface** → update its docstring in the same commit.
7. **Speed from experience, never transplanted code** — re-derive, don't copy-paste.
8. **`DECLARATION.md`** committed to both repos by Sun 13:00.
