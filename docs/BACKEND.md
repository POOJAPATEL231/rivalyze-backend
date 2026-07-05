# Rivalyze Backend — Developer Reference

> One document to understand the whole backend: architecture, API surface, auth, the two-phase
> agent pipeline, data layer, configuration, testing, and deployment.
> Companion docs (linked, not duplicated): [data_dictionary.md](data_dictionary.md) ·
> [llm_prompts.md](llm_prompts.md) · [FRONTEND_STATS_INTEGRATION.md](FRONTEND_STATS_INTEGRATION.md) ·
> [FRONTEND_IDEA_INTAKE.md](FRONTEND_IDEA_INTAKE.md)

Rivalyze is a multi-agent competitive-intelligence backend: give it a company (or just a startup
idea) and it discovers rivals, gathers news / product / review intelligence in parallel, fuses
everything into a cited evidence graph, and produces a typed competitive report — with follow-up
monitoring ("what's new since last run"), history, exports, and chat.

**Stack:** Python 3.12 · FastAPI · LangGraph-style two-phase agent graph · psycopg 3 → Azure
PostgreSQL Flexible Server · Redis (cache + rate-limit storage) · 4-provider LLM router
(Groq / Gemini / Cerebras / OpenRouter) · Tavily + Serper search · Docker → Azure Web App.

---

## 1. Architecture at a glance

```
                       POST /analyze (company OR idea)
                                   │
                        ┌──────────▼──────────┐
              Phase 1   │      discovery      │  idea-mode: idea → company+domain pre-step
                        └──────────┬──────────┘
                                   │ parks at awaiting_confirmation
                        user edits rival list, POST /runs/{id}/confirm
                                   │
              Phase 2   ┌──────────▼──────────┐
                        │  news ‖ product ‖ review   (parallel gather agents)
                        └──────────┬──────────┘
                        ┌──────────▼──────────┐
                        │        merge        │  deterministic: signals + evidence rows (cited)
                        └──────────┬──────────┘
                        ┌──────────▼──────────┐
                        │     strategist      │  report draft; CODE recomputes confidence/citations
                        └──────────┬──────────┘
                        ┌──────────▼──────────┐
                        │      validate       │  honesty gate; one repair retry; degraded shell
                        └──────────┬──────────┘
                                   ▼
                 Postgres: runs · signals · evidence · reports
                                   │
        GET /reports/{run_id} · /history (has_new) · /companies/{slug}/delta · /chat
```

Everything durable lives in Postgres — the process holds no run state, so restarts are safe.
With no database configured, an in-memory `_MemStore` fallback keeps the run lifecycle working
for local/offline dev (auth and signals remain DB-only).

## 2. Repository layout

```
app/
  main.py                 app assembly: CORS, security headers, routers, rate limiter, / UI
  models.py               THE pydantic contract (domain + API models) — owner: Drashti
  api/
    routes.py             frozen /api/v1 contract: analyze, runs, confirm, reports, evidence, health
    auth_routes.py        signup / login / refresh / logout / me
    history_routes.py     history (has_new flag) + markdown export
    companies_routes.py   Monitor Delta: {slug}/delta + {slug}/refresh
    chat_routes.py        ask-about-a-company chat (background + poll)
  core/
    lifecycle.py          two-phase run state machine + event emitter + lane_stats
    orchestrator.py       discovery/analysis graphs, node boundaries, validate gate
    llm_router.py         4-lane LLM failover router (extract / reason task classes)
    search_chain.py       cache → Tavily → Serper → scrape chain
    merge.py              deterministic signal + evidence fusion (not an LLM)
    auth.py, security.py  require_token / get_current_user; bcrypt + JWT + refresh tokens
    config.py             ALL env parsing (except provider keys, read lazily)
    cache.py, counters.py Redis+PG write-through cache; per-provider daily counters
    delta.py, stats.py    Monitor Delta dedupe; deterministic report stats
    confidence.py         code-computed recommendation confidence
    grounding.py          URL grounding helpers (anti-hallucination)
    intel_cache.py        per-rival gather cache (feature-flagged)
    report_eval.py        optional LLM-as-judge report scoring (feature-flagged)
    export.py             CompetitiveReport → markdown
    ratelimit.py          slowapi limiter (auth endpoints)
    user_store.py, refresh_store.py   Postgres-only auth persistence
  agents/
    discovery.py  news.py  product.py  review.py  strategist.py  idea.py
  db/
    connection.py         psycopg3 pool (DATABASE_URL or PG* env; sslmode=require)
    repository.py         frozen data-access signatures + _MemStore fallback — owner: Dharvi
    schema.sql            idempotent DDL (apply twice safely)
docs/                     this file + frontend integration guides + data dictionary + prompts
scripts/warmup.py         two-phase warm-up runner over seed/warmup_companies.json (15 companies)
scripts/check_cache.py    Redis/PG cache connectivity probe
static/index.html         minimal render-proof UI at /
budgets.json              per-provider daily call caps (soft guardrail)
tests/                    unit · boundary · contract · security · agents · db
```

## 3. Run modes & configuration

Config is read **once at import** in [app/core/config.py](../app/core/config.py) (after
`app/__init__.py` loads `.env`). Provider API keys are deliberately NOT in config — the router
and search chain read them lazily so a missing lane is skipped, not a startup crash.

| Env var | Default | Purpose |
|---|---|---|
| `MOCK_MODE` | `0` | `1` = deterministic offline lanes, zero keys needed (tests pin this) |
| `AUTH_DISABLED` | `0` | explicit dev opt-out for auth (never in prod) |
| `DEMO_RESERVE` | `0` | reserved flag (defined + exposed, currently consumed nowhere) |
| `BEARER_TOKEN` | `""` | static API token; empty serves open ONLY under MOCK/AUTH_DISABLED, else **fail-closed 503** |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | the single allowed CORS origin |
| `DATABASE_URL` / `PGHOST`+`PG*` | — | Postgres (either form; TLS forced) |
| `REDIS_URL` | `""` | cache hot layer + rate-limit storage (`rediss://` for Azure) |
| `JWT_SECRET` | random per-process | HS256 signing key. Unset = ephemeral (tokens die on restart) + warning |
| `JWT_EXPIRE_MINUTES` | `60` | access-token TTL |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | refresh-token TTL |
| `RATE_LIMIT_ENABLED` | `1` | auth-endpoint throttling (`0` in tests) |
| `RATELIMIT_SIGNUP/LOGIN/REFRESH` | `5/min`, `10/min`, `10/min` | per-IP limits |
| `RATELIMIT_STORAGE_URI` | REDIS_URL or `memory://` | limiter backend |
| `RICH_SEARCH` | `0` | deeper search: 6 results/query, `advanced` depth, 12 000-char corpus (vs 3/basic/6 500) |
| `GATHER_CONCURRENCY` | `1` | competitors processed at once by news/product/review |
| `COMPETITOR_INTEL_CACHE` / `_TTL` | `0` / `86400` | per-rival gather cache (reuse for newly added rivals) |
| `REPORT_EVAL` | `0` | LLM-as-judge scores the finished report into `lane_stats.report_score` |

**Provider keys** (lazy; each supports a **comma-separated multi-key list** — one env var / one
Azure Key Vault secret per provider, keys rotated on 4xx): `GROQ_API_KEY`, `GEMINI_API_KEY`,
`CEREBRAS_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`, `SERPER_API_KEY`.
Model overrides: `GROQ_EXTRACT_MODEL`, `GROQ_REASON_MODEL`, `GROQ_DEEPSEEK_MODEL`,
`GEMINI_MODEL`, `CEREBRAS_MODEL`, `CEREBRAS_MODEL_ALT`, `OPENROUTER_EXTRACT_MODEL`, `OPENROUTER_REASON_MODEL`.

**Run it:**
```bash
pip install -r requirements.txt
MOCK_MODE=1 uvicorn app.main:app --port 8000          # offline, zero keys
# PowerShell: $env:MOCK_MODE="1"; uvicorn app.main:app --port 8000
```

## 4. API reference

All routes mount under `/api/v1`. **Auth column:** `token` = `require_token` (static
`BEARER_TOKEN` **or** any valid user JWT; open in MOCK/AUTH_DISABLED) · `JWT` = strict
`get_current_user` · `open` = none.

| Method | Path | Auth | Purpose / notable responses |
|---|---|---|---|
| POST | `/analyze` | token | Start a two-phase run (company or idea). `?cached=true` opts in to instantly return the last completed report. 422 if neither company nor idea. |
| POST | `/analyze/company` | token | Company+domain-only sibling. |
| POST | `/analyze/idea` | token | Idea-only sibling; accepts optional structured intake (see [FRONTEND_IDEA_INTAKE.md](FRONTEND_IDEA_INTAKE.md)). |
| GET | `/runs/{job_id}` | token | Poll shape (`RunStatus`); parks at `awaiting_confirmation` with the proposed rival set. 404 unknown. |
| POST | `/runs/{job_id}/confirm` | token | Phase-2 launch ("Deploy the agents"). 202; 404 unknown; **409** if not awaiting confirmation (atomic CAS — agents launch exactly once). |
| GET | `/reports/{run_id}` | token | Full `CompetitiveReport` (incl. optional `stats`). 404 if absent. |
| GET | `/reports/{run_id}/export?format=md` | token | Markdown attachment (cached in `reports.md_export`). 400 non-md. |
| GET | `/evidence/{claim_ref}?run_id=` | token | Citation drawer. Unknown claim_ref = valid 200 empty; 404 only if the run is unknown. |
| GET | `/evidence-refs?run_id=&ids=` | token | Evidence rows by comma-separated `ev-` ids (filtered to the run). |
| GET | `/history?company=&limit=` | token | Completed runs newest-first (ILIKE filter, limit ≤ 100). Each company's **newest** row carries **`has_new: bool`** — the "new changes" popup trigger. |
| GET | `/companies/{slug}/delta` | token | Monitor Delta: what's new vs the previous run. Two 200 shapes (full delta / `first_run: true`); 404 unknown slug. **Pure DB read — no AI, no credits.** |
| POST | `/companies/{slug}/refresh` | token | Monitoring re-run: reuses the last confirmed rival list (no discovery, no gate), 202 + job_id. 409 if no completed run with confirmed rivals. |
| POST | `/chat` | token | Ask about a company (background task); GET `/chat/{chat_id}` polls answer/events/source (stored/live/mixed). |
| POST | `/auth/signup` | open (5/min) | 201 + token pair; 409 email exists (incl. TOCTOU race). |
| POST | `/auth/login` | open (10/min) | Token pair; generic 401 + dummy bcrypt verify (no user enumeration / timing leak). |
| POST | `/auth/refresh` | open (10/min) | **Rotates** the refresh token. Replaying a revoked token = theft ⇒ revokes the user's whole token family. |
| POST | `/auth/logout` | open | 204, idempotent revoke. |
| GET | `/auth/me` | **JWT** | Caller identity (`user_id`, `email`). |
| GET | `/health` | open | `{"status":"ok","service":"rivalyze"}` |
| GET | `/health/cache`, `/health/evidence` | open | Live Redis/PG/evidence diagnostics; never raise, leak no secrets. |
| GET | `/` | open | Minimal demo UI (`static/index.html`). |

**Monitoring loop (frontend recipe):**
`POST /companies/{slug}/refresh` → poll `GET /runs/{job_id}` to `completed` →
`GET /companies/{slug}/delta` for the panel; `GET /history` rows with `has_new: true` drive the badge/popup.

## 5. Authentication & security

Two auth levels ([app/core/auth.py](../app/core/auth.py)):

- **`require_token`** — service gate on contract routes. Accepts the static `BEARER_TOKEN`
  (constant-time compare) **or** a valid user JWT. **Fail-closed:** if no `BEARER_TOKEN` is
  configured and the run isn't explicitly dev (`MOCK_MODE`/`AUTH_DISABLED`), it returns **503**
  rather than silently serving open.
- **`get_current_user`** — strict identity: valid unexpired JWT whose `sub` maps to a known user.

**Tokens** ([app/core/security.py](../app/core/security.py)):
- Access token: **HS256 JWT**, claims `sub`/`email`/`iat`/`exp`, stateless (stored in no table).
- Refresh token: high-entropy opaque string (`token_urlsafe(32)`), stored as **SHA-256 hash
  only**, 30-day expiry, **rotated on every use**; replaying a revoked token revokes the whole
  family (theft assumption). Rows are flagged `revoked`, never deleted, so replay is detectable.
- Passwords: **bcrypt** with per-password salt; input truncated to 72 bytes identically on hash
  and verify; a precomputed `DUMMY_HASH` keeps the unknown-email login path as slow as the real
  one (timing-attack / enumeration mitigation). Models reject > 72-byte passwords with a 422.

**Hardening** (asserted by `tests/security/`): generic 401s with `WWW-Authenticate: Bearer`
(never reveal absent vs expired vs malformed); CORS locked to `FRONTEND_ORIGIN` with
`allow_credentials=False` and pinned methods/headers; security headers on every response
(`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
`Cross-Origin-Opener-Policy: same-origin`); input length caps + control-char stripping on all
analyze inputs; path-safe job_id slugs; per-IP rate limits on signup/login/refresh (Redis-backed
when `REDIS_URL` is set); SSRF guard + robots.txt check on the scrape fallback; prompt-injection
guards in agent prompts with typed output caps as the hard backstop.

## 6. The two-phase pipeline

State machine ([app/core/lifecycle.py](../app/core/lifecycle.py)), all transitions persisted:

```
queued → running_discovery → awaiting_confirmation → confirmed → running_analysis → completed
                                                                                  ↘ failed
```

- **Phase 1 — discovery.** `start_run` creates company + run rows
  (`job_id = rivalyze-{slug}-{6hex}`), returns immediately; discovery runs as a FastAPI
  BackgroundTask and parks at `awaiting_confirmation` with a proposed rival set (≤ 4). Idea mode
  first resolves the idea into a real company + domain and persists the resolved identity so the
  report is stamped with the company name, not the idea sentence.
- **The gate.** The user edits the rival list in the UI; `POST /confirm` uses an **atomic
  compare-and-swap** (`repository.confirm_run`) so exactly one caller wins — a double confirm is
  a 409 and the agents never double-run.
- **Phase 2 — analysis.** `news ‖ product ‖ review` run in parallel (additive-reducer state
  fields, so siblings never clobber), then `merge` → `strategist` → `validate`.
- **Node boundaries** ([app/core/orchestrator.py](../app/core/orchestrator.py)): every node is
  wrapped — an exception or malformed output becomes a typed-empty "degraded delta" plus a
  `low_signal_findings` entry, never a crashed run. The **validate** node is the honesty gate
  (rejects markdown fences / JSON-shaped summaries / empty fields) and grants the strategist
  exactly one repair retry; still failing ⇒ an honest **degraded report shell** persists so
  `GET /reports/{run_id}` returns 200, never 404.
- **Events & metrics.** Every agent emits `(agent, msg)` events appended atomically to
  `runs.events`; an emitter aggregates `llm_calls / searches / cache_hits / signals_found /
  evidence_rows` into `runs.lane_stats` (throttled writes ≥ 1.5 s; phase 2 seeds from phase 1 so
  counts accumulate).
- **Persistence-first.** Re-analyzing a known company returns the stored report **only when
  requested** (`?cached=true`); the default runs fresh so monitoring gets a genuine new run.

## 7. Agents

Common pattern: search → corpus (capped) → LLM extract (lenient inner schema) → **ground /
sanitize in code** → typed output. Thin corpus (< 300 chars), zero grounded survivors, or lane
exhaustion ⇒ `low_signal=True` result — agents never raise. Prompts live in
[llm_prompts.md](llm_prompts.md).

| Agent | Output | Notable behavior |
|---|---|---|
| `discovery` | `CompetitorSet` (≤ 4) | 3 concurrent queries; self-exclusion; generic giants flagged; injection-guarded prompt |
| `news` | `list[NewsSignals]` | items kept ONLY if `source_url` appears verbatim in the corpus (grounding); ISO-date scrub; ≤ 10 items |
| `product` | `list[ProductIntel]` | pricing/vs-you/features/positioning queries; plain-string pricing tiers enforced; URL grounding |
| `review` | `list[SentimentIntel]` | complaints/reddit/app-store queries; flattens accidental dicts; sentiment coerced to enum; ≤ 8 |
| `strategist` | `CompetitiveReport` | reason lane. **Code owns numbers & citations**: unknown evidence ids stripped, confidence recomputed from cited evidence (`confidence.py`), ≤ 3 recs, deterministic `ReportStats` attached (`stats.py`) |
| `idea` | company + domain | idea → searchable company identity; deterministic geography/industry folding; heuristic fallback, never raises |

**Merge** ([app/core/merge.py](../app/core/merge.py)) is deterministic code, not an LLM: fuses
the three lanes into typed `Signal`s + cited `EvidenceRow`s (`ev-` ids, ≤ 280-char snippets),
persists both best-effort, and emits the `fused N signals · M evidence rows` line the metrics
parse.

## 8. LLM router

[app/core/llm_router.py](../app/core/llm_router.py) — one code path, four providers, all spoken
in the OpenAI chat-completions dialect. `complete(task_class, prompt, schema, emit)` returns a
**validated pydantic object** + the lane that produced it.

- **Task classes:** `extract` (fast/cheap: Groq 8B → Gemini Flash → Cerebras ×2 → OpenRouter)
  and `reason` (CoT: Groq qwen3-32b → Gemini → Cerebras → Groq 70B → OpenRouter DeepSeek).
- **Failover:** any **4xx is treated as key-specific** → rotate to the provider's next
  comma-separated key first, then fail over to the next lane (no sleep on 429s — concurrent
  callers would pile up). 5xx → short exponential backoff. Parse failure → one JSON-repair pass
  (strips `<think>` blocks / fences, outermost `{}` slice) then lane failover.
- **Budgets:** `budgets.json` daily caps per lane, tracked in Redis via `counters.py`
  (`credits:<provider>:<date>`, self-resetting keys). Over-budget lanes are skipped — unless
  *every* lane is over, in which case the cap yields (soft guardrail; a run never returns empty
  because of budgeting). `MOCK_MODE` routes to a deterministic offline mock lane keyed on prompt
  markers, which echoes real `SOURCE:`/`ev-` tokens so grounding filters pass.

## 9. Search chain & caching

[app/core/search_chain.py](../app/core/search_chain.py): `cache → Tavily → Serper → scrape → []`
(empty result = "low signal", never an error). `search_all` fans queries out over a thread pool.
The scrape fallback only fires for competitor-domain queries and is SSRF-guarded (public IPs
only, robots.txt honored, no off-domain redirects, 2 000-char cap).

[app/core/cache.py](../app/core/cache.py): Redis hot layer (24 h TTL) with **Postgres
write-through** (`search_cache` table, 7-day staleness window) so cached corpus survives a Redis
flush. Everything degrades to a cache-miss, never an exception. `GET /api/v1/health/cache` is
the live diagnostic; `python scripts/check_cache.py` the offline probe.

## 10. Monitor Delta ("what's new since last run")

Feature doc: `Rivalyze_Monitor_Delta_Backend.md` (team drive). Pieces:

- **[app/core/delta.py](../app/core/delta.py)** — pure logic. A signal in the latest run is NEW
  iff no signal in the previous run shares the identity key
  `(agent, competitor, type, normalized_headline)` where the headline is lowered, stripped to
  `[a-z0-9 ]`, trimmed, first 80 chars. Rewordings of the same event therefore do **not** count.
- **`GET /companies/{slug}/delta`** — diffs the two latest completed runs at read time. Nothing
  new is stored; no AI is called.
- **`GET /history` → `has_new`** — computed per company's newest row on read.
- **`POST /companies/{slug}/refresh`** — the "week 2" trigger: re-runs analysis on the
  previously confirmed rivals (no gate). In production this same path becomes the weekly Azure
  Functions timer; email delivery remains roadmap.

## 11. Data layer

Schema ([app/db/schema.sql](../app/db/schema.sql), idempotent — apply twice safely; full prose in
[data_dictionary.md](data_dictionary.md)):

| Table | Purpose / key columns |
|---|---|
| `companies` | `slug` unique, `lower(name)` unique, `is_hero` |
| `runs` | `job_id` unique, `status`, `current_stage`, `events jsonb`, `lane_stats jsonb`, `threat_level`, `report_confidence`; idx `(company_id, status)` |
| `reports` | 1:1 with runs; full report jsonb + cached `md_export` |
| `competitors` | per-run confirmed rival list |
| `signals` | per-run typed findings (`agent`, `competitor`, `type`, `payload jsonb`, `evidence_ids`); idx `(run_id)` |
| `evidence` | `ev-` text PK, `claim_ref`, source fields, ≤ 280-char snippet; idx `(run_id, claim_ref)` |
| `users` / `refresh_tokens` | bcrypt hash; sha256 refresh hashes, `revoked` flag (never deleted) |
| `search_cache` | write-through search corpus cache |
| `kb_stores` / `documents` | chat knowledge-base bookkeeping (heroes never LRU-evicted) |

[app/db/repository.py](../app/db/repository.py) owns **frozen function signatures** (parameterized
SQL only, no ORM; uuids returned as `id::text`; jsonb round-trips as dict/list). 23 run-lifecycle
functions are wrapped with an in-memory `_MemStore` fallback for no-DB dev; **DB-only by design:**
auth stores, search cache, and the delta reads (`get_company_by_slug`,
`get_latest_completed_runs`, `get_signals_for_run`).
[app/db/connection.py](../app/db/connection.py) builds one lazy pool (max 5, TLS forced) from
`DATABASE_URL` or `PG*` env.

## 12. Testing

```bash
MOCK_MODE=1 pytest -q          # offline; DB-backed suites auto-skip without DATABASE_URL/PG*
RATE_LIMIT_ENABLED=0 pytest -q # (tests disable the limiter via conftest fixture anyway)
```

- **`tests/conftest.py` pins `MOCK_MODE=1` before anything imports `app`** — this is
  load-bearing: without it, the first collected test file that imports `app` would load `.env`'s
  `MOCK_MODE=0` and silently flip the whole suite into real-provider calls. Run a deliberate
  real-mode suite with `MOCK_MODE=0 pytest`.
- Suites: `unit/` (pure logic — delta, stats, router failover/key rotation via respx, grounding,
  export, lifecycle fixes on `_MemStore`), `boundary/` (graph nodes with stubbed agents; repo
  writes no-op'd), `contract/` (real ASGI TestClient, real Postgres; module-level
  `skipif not connection.is_enabled()`), `security/` (auth fail-closed, headers, CORS, injection,
  input caps), `agents/` (MOCK-lane regression guards, grounding ladders), `db/` (repository
  round-trips). ~239 tests.
- Conventions: unique slugs per test + `DELETE FROM companies` cascade cleanup; hardcoded ids
  never touch a shared DB (in-memory pinning); generous 30 s poll ceilings for the shared Azure
  PG.

## 13. Deployment & operations

- **Dockerfile:** `python:3.12-slim`, non-root user, `uvicorn app.main:app` on `$PORT` (8000;
  pair with `WEBSITES_PORT=8000` on Azure).
- **CI** (`.github/workflows/ci.yml`, PRs → main): ruff, bandit (`-ll`), pip-audit,
  `MOCK_MODE=1 pytest -q`.
- **Dev deploy** (`deploy-dev.yml`, push → main): OIDC Azure login → buildx → push to
  `rivalyzeregistry.azurecr.io` → `az webapp config container set` + restart on
  `rivalyze-backend-webapp-cin-01` (rg `rg-rivalyze-cin-01`) → health gate (15 × curl
  `$APP_HEALTH_URL`, expects 200). `deploy.yml` (tag-triggered publish-profile deploy) is the
  older placeholder path.
- **Warm-up:** `python -m scripts.warmup --base <url> --token <bearer>` drives the full
  two-phase flow (auto-approving the gate) over `seed/warmup_companies.json` and writes a
  manifest — this loop is the shape of the future weekly scheduler.
- **Secrets:** one Azure Key Vault secret per provider holding the comma-separated key list
  (mirrors the env-var convention). `JWT_SECRET`, `BEARER_TOKEN`, `DATABASE_URL`, `REDIS_URL`
  must be injected in any shared deployment — remember auth **fails closed** without them.

## 14. Gotchas & invariants (read before changing things)

1. **`app/models.py` and `app/db/repository.py` signatures are frozen contracts** — changes are
   cross-team announcements, not refactors.
2. **Never hardcode a JWT fallback secret**; the ephemeral path exists only for local dev.
3. **Signals/evidence/auth have no in-memory fallback** — features built on them are DB-only;
   guard with `connection.is_enabled()` where a no-DB path exists.
4. **The delta endpoint must stay AI-free** — `has_new` runs on every history view; cost there
   multiplies.
5. **Code, not the LLM, owns confidence numbers and citation validity** (strategist post-pass).
6. **Agents never raise** — a failure is a typed `low_signal` result or a degraded delta, so one
   bad rival/lane never sinks a run.
7. **4xx from a provider = try the next key, then the next lane. Never sleep-retry a 429.**
8. New gathering agent? Add its MOCK-router branch **and** a `tests/agents/test_mock_extraction.py`
   case in the same PR (regression guard).
9. Any new test file that imports `app` inherits `MOCK_MODE` from root `conftest.py` — do not
   remove that pin.
