# Rivalyze Backend — Installation & Setup Guide

> From `git clone` to a running backend. Three setup tiers — pick the one you need:
> **Tier 1** runs in 2 minutes with zero keys; **Tier 3** is the full production shape.
> Reference docs: [BACKEND.md](BACKEND.md) (internals) · [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md).

## Prerequisites

| Requirement | Version | Needed for |
|---|---|---|
| Python | **3.12** | always |
| pip + venv | bundled | always |
| PostgreSQL | 14+ (Azure Flexible Server in prod) | Tier 2/3 — auth, history, delta, persistence |
| Redis | any (Azure Cache for Redis in prod) | optional — hot cache, shared rate limits, budget counters |
| Docker | 24+ | optional — containerized runs / parity with production |

## 1. Clone & install

```bash
git clone https://github.com/POOJAPATEL231/rivalyze-backend.git
cd rivalyze-backend
python -m venv .venv
source .venv/bin/activate            # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2. Choose your setup tier

### Tier 1 — Offline / zero keys (fastest start, ~2 min)

No database, no API keys, no `.env`. The LLM router and search chain run deterministic
MOCK lanes; the run lifecycle uses an in-memory store.

```bash
MOCK_MODE=1 uvicorn app.main:app --port 8000
# PowerShell:  $env:MOCK_MODE="1"; uvicorn app.main:app --port 8000
```

Open <http://localhost:8000>, hit **Analyze**, watch the live event ledger.
Limits of this tier: no signup/login, no history/delta (those persist to Postgres only).

### Tier 2 — Live development (database + any subset of keys)

```bash
cp .env.example .env     # then fill in what you have
```

Minimum useful `.env` for development:

```ini
# database — EITHER a full DSN...
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require
# ...OR libpq-style vars (the app accepts either form; TLS is forced on):
# PGHOST=... PGUSER=... PGPASSWORD=... PGDATABASE=... PGPORT=5432

# user auth — generate once:  python -c "import secrets;print(secrets.token_urlsafe(32))"
JWT_SECRET=<generated>

# any SUBSET of provider keys — the router simply skips lanes whose key is missing.
# Multiple keys per provider: comma-separate them (auto-rotation on rate limits).
GROQ_API_KEY=
GEMINI_API_KEY=
CEREBRAS_API_KEY=
OPENROUTER_API_KEY=
TAVILY_API_KEY=
SERPER_API_KEY=

MOCK_MODE=0
```

Apply the schema (idempotent — safe to run twice):

```bash
psql "$DATABASE_URL" -f app/db/schema.sql
```

Run and verify:

```bash
uvicorn app.main:app --port 8000
curl http://localhost:8000/api/v1/health           # {"status":"ok","service":"rivalyze"}
curl http://localhost:8000/api/v1/health/cache     # Redis/Postgres cache diagnostic
curl http://localhost:8000/api/v1/health/evidence  # evidence persistence diagnostic
```

### Tier 3 — Production shape (everything on)

Everything from Tier 2, plus:

```ini
BEARER_TOKEN=<strong random token>       # REQUIRED in prod — without it (and without
                                         # MOCK/AUTH_DISABLED) the API fail-closes with 503
REDIS_URL=rediss://:KEY@NAME.redis.cache.windows.net:6380/0
FRONTEND_ORIGIN=https://your-frontend.example    # the ONLY origin CORS will allow
RATE_LIMIT_ENABLED=1
```

Secrets management (Azure): **one Key Vault secret per provider**, its value the
comma-separated key list — mirrors the env convention exactly, no code changes to add keys.

## 3. Verify the install

```bash
# offline test suite (DB-backed suites auto-skip without a database)
MOCK_MODE=1 pytest -q

# with a database configured, the full ~239-test suite runs:
pytest -q
```

Then exercise the real flow end to end:

```bash
# 1. create a user, grab the JWT
curl -X POST localhost:8000/api/v1/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"dev@example.com","password":"Str0ngPass!x"}'

# 2. start an analysis (Bearer = the access_token from step 1, or BEARER_TOKEN)
curl -X POST localhost:8000/api/v1/analyze \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"company":"Notion","domain":"workspace software"}'

# 3. poll GET /api/v1/runs/{job_id} → confirm rivals → poll to completed → GET /reports/{run_id}
```

## 4. Docker

```bash
docker build -t rivalyze-backend .
docker run --rm -p 8000:8000 --env-file .env rivalyze-backend
# or fully offline:  docker run --rm -p 8000:8000 -e MOCK_MODE=1 rivalyze-backend
```

Image details: `python:3.12-slim`, non-root `appuser`, serves on `$PORT` (default 8000 —
set `WEBSITES_PORT=8000` on Azure Web App).

## 5. Deployment (already automated)

Merging to `main` triggers `.github/workflows/deploy-dev.yml`: OIDC login to Azure →
image build & push to `rivalyzeregistry.azurecr.io` → container swap + restart on the
Azure Web App → **health gate** (the deploy fails if `/api/v1/health` doesn't return 200).
No manual deploy steps; tag `v*` releases use `deploy.yml`.

Post-deploy warm-up (optional, seeds demo companies through the full two-phase flow):

```bash
python -m scripts.warmup --base https://<your-app>.azurewebsites.net --token <BEARER_TOKEN>
```

## 6. Configuration quick reference

Full table with every flag: [BACKEND.md §3](BACKEND.md#3-run-modes--configuration). The ones
that matter at install time:

| Variable | Required? | Notes |
|---|---|---|
| `MOCK_MODE` | Tier 1 only | `1` = fully offline, zero keys |
| `DATABASE_URL` **or** `PG*` | Tier 2+ | auth/history/delta refuse to run without it |
| `JWT_SECRET` | Tier 2+ | unset = ephemeral per-process secret (tokens die on restart) — dev only |
| `BEARER_TOKEN` | Tier 3 | unset in non-mock mode ⇒ API fail-closes (503) by design |
| Provider keys | any subset | missing key = that lane is skipped, not an error |
| `REDIS_URL` | optional | cache + shared rate limits; app degrades gracefully without it |

## 7. Troubleshooting

| Symptom | Cause → fix |
|---|---|
| Every endpoint returns **503 "server auth is not configured"** | Non-mock run with empty `BEARER_TOKEN` — intended fail-closed. Set `BEARER_TOKEN`, or `MOCK_MODE=1` / `AUTH_DISABLED=1` for local dev. |
| **401 on every request after a server restart** | `JWT_SECRET` unset ⇒ ephemeral per-process secret; old tokens can't verify. Set a fixed `JWT_SECRET` in `.env`. |
| Signup/login return **500 / "no database configured"** | Auth is Postgres-only. Set `DATABASE_URL` or `PG*` and apply `schema.sql`. |
| `psycopg` SSL errors against Azure | Azure Flexible Server requires TLS — keep `sslmode=require` (the app forces it if omitted). |
| **429 Too Many Requests** on login/signup while testing | Per-IP rate limits (5/min signup, 10/min login). `RATE_LIMIT_ENABLED=0` for local testing. |
| Reports come back `low_signal` with real keys set | Check per-lane daily budgets (`budgets.json` + Redis counters) and key validity — the router skips exhausted/invalid lanes. `GET /health/cache` + logs show lane events. |
| Tests hit real providers / burn credits | Never remove the `MOCK_MODE` pin in `tests/conftest.py` — it's what keeps the suite offline. |
