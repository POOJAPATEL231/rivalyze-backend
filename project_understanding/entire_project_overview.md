# Rivalyze — Entire Project Overview
## Team: Argus | Event: CodeClash 2026

---

## What Is This Product?

Rivalyze is a competitive intelligence tool. You type in the name of a company (like "Notion" or "Zomato") and it automatically researches all its competitors on the internet, then gives you a detailed report — who the rivals are, what they're doing, how their products are priced, what customers are complaining about, and what your company should do next.

Think of it like having 5 AI agents go out on the internet simultaneously, research everything about your competitors, and come back with a polished strategy report in a few minutes.

---

## What Problem Does It Solve?

Competitive research normally takes days. Rivalyze automates it end-to-end — from "here's my company" to a full board-ready report with evidence, confidence scores, and strategic recommendations — in minutes.

---

## The Tech Stack (Simple Version)

- **Backend:** Python with FastAPI (the server that does all the AI work)
- **Frontend:** React with TypeScript and Tailwind (the website the user sees)
- **Database:** Azure PostgreSQL (stores all runs, results, and evidence)
- **Cache:** Azure Redis (remembers search results so we don't waste API credits)
- **AI Models:** Gemini, Groq, Cerebras, OpenRouter — multiple fallbacks so it never goes down
- **Search APIs:** Tavily, then Serper, then direct website scraping as a last resort
- **Hosting:** Azure App Service (backend) + Azure Static Web Apps (frontend)

---

## How The System Works — Step by Step

1. User types a company name on the website and clicks "Start Intelligence Scan"
2. The backend immediately gives back a job ID and starts working in the background
3. A "Discovery Agent" searches the web and finds up to 4 competitors
4. Three agents run IN PARALLEL:
   - **News Agent** — finds recent launches, funding, news about each rival
   - **Product Agent** — finds pricing tiers, features, positioning of each rival
   - **Reviews Agent** — mines complaints on Reddit, review sites, forums
5. A **Merge step** collects all this data and creates "evidence rows" (traceable sources)
6. A **Strategist Agent** synthesizes everything into a CompetitiveReport with SWOT, opportunities, and recommendations
7. A **Validate step** double-checks the output quality
8. The user sees the live progress on screen and then gets the full report

---

## The 7-Node Pipeline

```
Discovery → [News | Product | Reviews] → Merge → Strategist → Validate
              (these 3 run in parallel)
```

---

## What The Report Contains

- **Threat Level**: LOW / MEDIUM / HIGH / CRITICAL
- **Executive Summary**: 3 key points (Top Threat, Biggest Opportunity, Recommended Action)
- **Head-to-Head Table**: All rivals vs your company with NEW badges for fresh data
- **SWOT Analysis**: Strengths, Weaknesses, Opportunities, Threats
- **Sentiment Bars**: How customers feel about each rival (width = actual data)
- **Opportunities**: Specific gaps to exploit, each backed by evidence
- **Recommendations**: Up to 3 concrete actions, each with a confidence score and citations
- **Evidence Drawer**: Click any claim → see exactly which URLs prove it

---

## The Frozen API Contract (What The Backend Exposes)

| Endpoint | What It Does |
|---|---|
| POST /api/v1/analyze | Submit a company, get a job_id instantly |
| GET /api/v1/runs/{job_id} | Poll for status, live events, lane stats |
| GET /api/v1/reports/{run_id} | Get the final CompetitiveReport |
| GET /api/v1/evidence/{claim_ref}?run_id= | Get sources behind any claim |
| GET /api/v1/history | See past runs |
| GET /api/v1/health | Health check (always open, no auth) |

This contract is FROZEN as of Saturday 11:00 — nothing can change after that.

---

## The 17-Person Team & Who Does What

### BACKEND POD (the engine)

| Person | Role | What They Build |
|---|---|---|
| **Drashti** (Lead) | API + Lifecycle | The FastAPI routes, run lifecycle, auth, contract enforcement. Reviews everything. |
| **Sheel** | Discovery + Strategist + RAG | The Discovery agent (first real e2e by Sat 13:00), Strategist agent (the quality-critical synthesis), and the RAG/knowledge layer for document upload + grounded chat |
| **Virat** | Orchestrator + News Agent | Wires the LangGraph pipeline (7 nodes), builds the News agent, and the Idea-to-domain pre-step |
| **Gati** | Merge + Confidence + Evidence | THE differentiator — turns all agent output into traceable evidence rows, computes the confidence formula, wires the evidence drawer endpoint |
| **Tushar** | Search Chain + Counters + Product Agent | Builds the search layer (Tavily → Serper → scraper), credit counters for API usage, and the Product agent (pricing/features/positioning) |
| **Mihir** | Cache + Router Hardening + Reviews Agent | Builds the Redis cache layer, hardens the LLM router (budgets, failover, demo reserve), builds the Reviews agent |
| **Dharvi** | Database + Repository + History + Warmup | Builds FIRST (everyone depends on her). Creates the SQL schema, all database functions, history endpoint, markdown export, and the cache warm-up script |

### FRONTEND POD (what the user sees)

| Person | Role | What They Build |
|---|---|---|
| **Dhwani** (Lead) | Foundation + Brief/Discovery views | The shared design system, API client, polling hook, Demo Mode (for testing without a backend), the splash page, the Brief entry form, and the Discovery confirmation view |
| **Akash** | Run Monitor + History view | The live progress screen (5 lanes, event ledger, telemetry), the History list, and shared degraded/low-signal status components |
| **Krutarth** | Dashboard | The full report view — executive summary hero card, head-to-head table, SWOT quad, sentiment bars, opportunities, low signal footnote. Also Compare UI (stretch) |
| **Vatsal** | Evidence + Recommendations | The evidence drawer system (EvidenceChip, EvidenceDrawer, slide-over panel) and the Recommendations view with animated confidence rings |

### QA POD (quality assurance)

| Person | Role | What They Build |
|---|---|---|
| **Rushabh** | QA Lead | Owns TC-N01 (search fallback drill), TC-P02 (Redis flush harmless), runs the Tavily-revoke live test with Tushar on Sunday 10:30, screen-recording backup video |
| **Hely** | Contract Tests | Writes and runs TC-C01–C08 (API contract shape, auth, lifecycle, instant re-run) — these can be run TONIGHT on the POC |
| **Dhruv** | UI Tests (Playwright) | Writes TC-U01–U10 (pixel-faithful UI, drawer speed, no undefined/NaN), runs prerequisite verification sweep Saturday 10:00 |

### DEVOPS POD (infrastructure)

| Person | Role | What They Build |
|---|---|---|
| **Anupam** | CI/CD + Azure Deploy | GitHub Actions pipelines, Azure App Service deploy, backend CD, the disaster runbook |
| **Darshit** | Accounts + Data Services + Quota | All API key signups and custody, Azure PostgreSQL + Redis provisioning, Azure Static Web Apps for frontend, hourly quota reporting during the event |

### LEAD

| Person | Role |
|---|---|
| **Pooja** | Project Lead — coordinates all pods, quality bar for hero companies, manages the demo, presents to judges |

---

## Timeline (The Critical Path)

### Friday Evening (Tonight — Setup Only)
- Everyone: Install tools, clone repos, verify the POC runs in mock mode
- Sheel: Deploy hello-world to Azure App Service, test File Search RAG
- Darshit: Provision Azure PostgreSQL + Redis, fill the account matrix
- Hely: Write all TC-C (contract) test cases using the prototype
- Dhruv: Install Playwright (400MB download — must do tonight on good WiFi)

### Saturday
- **10:00** — Clone the POC, start building
- **10:30** — Dharvi applies the DB schema (everyone unblocked after this)
- **11:00** — CONTRACT FREEZES — no interface changes after this
- **11:30** — Virat starts the LangGraph orchestrator under Drashti's direction
- **13:00** — Sheel delivers first real end-to-end on Azure (Discovery live)
- **13:00** — Dhwani publishes frontend foundation + fixtures (frontend team unblocked)
- **14:00** — Router + DB live in the cloud; Tushar's search chain live
- **17:30** — Tushar starts the Product agent
- **18:00** — Vatsal publishes evidence components (Krutarth unblocked)
- **18:00** — First agent end-to-end
- **21:00** — CHECKPOINT: "hero" run fully working → stretch features gate opens
- **22:00** — Cache warm-up runs overnight (~15 hero companies)

### Sunday
- **08:30** — Tushar reports cache/credit stats; Dharvi runs evidence integrity queries
- **09:00** — Frontend connects to real API (not fixtures)
- **10:30** — Tushar + Rushabh drill the Tavily-revoke test live
- **11:00–13:00** — Tushar + Dharvi verify every evidence URL is live
- **12:00** — Rehearsal #1
- **13:30** — FEATURE FREEZE — demo-path fixes only after this
- **Demo** — Judges see it live

---

## Key Rules Everyone Follows

1. **Evidence or it didn't happen** — every claim the UI shows must trace to a real URL
2. **No uncomputed visuals** — if the data isn't there, the UI shows "low signal", never a made-up number
3. **Confidence is computed, not asserted** — the formula is `0.25 + 0.12*sources + 0.15*agreement + 0.10*agents`, clamped to [0.05, 0.95]
4. **Counters never break a run** — if Redis is down, the search still works; counters fail silently
5. **No ddgs** — DuckDuckGo is explicitly banned as a search provider
6. **Blocked >30 min? Escalate** — pair → pod lead → Pooja; silence is the only failure
7. **No new dependencies** without Drashti + Anupam approval

---

## The Three "Hero" Demo Companies

The team will pre-warm the cache overnight Saturday with analysis of real companies (Notion, Zomato, Razorpay are examples). The demo will show these live with real evidence rows, real confidence scores, and real source URLs so judges can click through and verify everything.

---

## What Makes This Impressive to Judges

1. **Every claim is evidence-backed** — click any sentence and see the source URL
2. **Confidence is computed math, not vibes** — the formula is shown in the drawer footer
3. **Multiple AI fallbacks** — if Gemini is down, Groq takes over automatically, shown live
4. **Search fallback drill** — Tushar + Rushabh revoke the Tavily key LIVE during the demo so judges see Serper take over in real time
5. **Persistence-first** — same company analyzed twice = instant result, zero new API calls
6. **5 AI agents in a 7-node pipeline** — merge and validate are deterministic code, not agents
