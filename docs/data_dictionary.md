# Data Dictionary — every table, every field (companion to schema.sql)
MS SQL translation once: uuid≈UNIQUEIDENTIFIER (gen_random_uuid≈NEWID) · text≈NVARCHAR(MAX) ·
jsonb≈NVARCHAR json but indexable+operators · timestamptz≈DATETIMEOFFSET · numeric(4,2)≈DECIMAL(4,2) ·
ILIKE≈case-insensitive LIKE · `events || $1::jsonb`≈JSON array append in ONE update.

**companies** — one row per analyzed company. Written by: analyze endpoint. Read by: find_completed_report (the persistence-first hit), history join. is_hero guards LRU + warm-up asserts.
**runs** — THE run lifecycle row; the polling endpoint reads ONLY this table. status drives the UI state machine; error is the one user-safe line (never a trace); events is append-only via a single jsonb-concat UPDATE (no read-modify-write races); lane_stats lands once at finish. Written by: lifecycle + every node's emit. Read by: GET /runs, history.
**reports** — the full CompetitiveReport as one jsonb (UNIQUE run_id = 1:1); md_export caches the first markdown render. Written by: finish. Read by: GET /reports, export, dashboard.
**competitors** — discovery's confirmed list per run (name/category/rationale) — feeds the Discovery view + judges' "show me the rivals" question.
**signals** — one row per typed agent finding with its evidence_ids array; payload keeps the original item verbatim (audit trail from finding → report).
**evidence** — THE differentiator table. id is code-generated text ("ev-…") because the strategist cites these ids inside report jsonb — text keys keep that join trivial. claim_ref is the UI's lookup key (index (run_id, claim_ref) serves the drawer in one seek). Sunday integrity query joins report→evidence proving zero dangling citations.
**search_cache** — Postgres half of the write-through cache (Redis is the hot layer; this survives flushes = TC-P02). key = sha256(lower(trim(query)))[:16].
**kb_stores** — Plan A bookkeeping: which company has a File Search store, when last used → the 10-cap LRU deletes MIN(last_used_at) WHERE is_hero=false. Also Plan B's collection registry.
**documents** — uploaded PDFs per store: powers the Workspace doc list ("indexed · 14 chunks") and the /documents response.
**embeddings** (Plan B only) — MiniLM 384-d chunks per company; 0.55 cosine floor enforced in code.
Deliberately ABSENT: users/auth (cut — single bearer token) · jobs table (job_id lives on runs) ·
migrations tooling (idempotent DDL is the weekend's migration story).
