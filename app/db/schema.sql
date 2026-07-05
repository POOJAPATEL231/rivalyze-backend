-- Rivalyze schema v1.0 (PostgreSQL 16, Azure Flexible Server) — idempotent, apply twice safely
-- Owner: Dharvi · Consumers: every repository function · MS SQL readers: see 00_data_dictionary.md

CREATE TABLE IF NOT EXISTS companies (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL,
  slug          text NOT NULL UNIQUE,              -- lower-kebab: "notion"
  domain        text,                              -- "connected workspace software"
  is_hero       boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_companies_lower_name ON companies (lower(name));

-- Auth users (added for the login/signup feature). Owner remains Dharvi (schema).
-- password_hash is a bcrypt hash — plaintext passwords NEVER touch this table.
CREATE TABLE IF NOT EXISTS users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text NOT NULL,
  password_hash text NOT NULL,                     -- bcrypt; never plaintext
  created_at    timestamptz NOT NULL DEFAULT now()
);
-- case-insensitive uniqueness: one account per email regardless of casing
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_lower_email ON users (lower(email));

-- Long-lived refresh tokens (rotation + revocation). token_hash is a SHA-256
-- of a high-entropy random token — the raw token is NEVER stored, only its
-- hash, so a DB leak cannot be replayed. Access tokens (JWT) are stateless and
-- live in NO table; only refresh tokens are persisted, because they must be
-- revocable (logout, rotation, theft detection).
CREATE TABLE IF NOT EXISTS refresh_tokens (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash  text NOT NULL UNIQUE,               -- sha256(raw token), never the raw value
  expires_at  timestamptz NOT NULL,
  revoked     boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_refresh_user ON refresh_tokens (user_id);

CREATE TABLE IF NOT EXISTS runs (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id             text NOT NULL UNIQUE,          -- "rivalyze-notion-a1b2c3"
  company_id         uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  user_id            uuid REFERENCES users(id) ON DELETE CASCADE,  -- owner; nullable so legacy rows survive
  status             text NOT NULL DEFAULT 'queued',-- queued|running|completed|failed
  current_stage      text NOT NULL DEFAULT 'queued',-- discovery|news|product|review|merge|strategist|validate|done
  threat_level       text,                          -- filled by finish_run
  report_confidence  numeric(4,2),                  -- 0.05–0.95
  error              text,                          -- one line, user-safe; NULL unless failed
  events             jsonb NOT NULL DEFAULT '[]',   -- append-only [{t,agent,msg}]
  lane_stats         jsonb NOT NULL DEFAULT '{}',   -- {"groq":11,"searches":9,...}
  started_at         timestamptz NOT NULL DEFAULT now(),
  finished_at        timestamptz
);
-- Idempotent migration for databases created before user_id existed: ADD COLUMN
-- IF NOT EXISTS is a no-op on fresh DBs (the CREATE above already has it) and a
-- one-time backfill of the column on existing ones. Safe to apply twice.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES users(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS ix_runs_job_id ON runs (job_id);
CREATE INDEX IF NOT EXISTS ix_runs_company_status ON runs (company_id, status);
CREATE INDEX IF NOT EXISTS ix_runs_user ON runs (user_id);

CREATE TABLE IF NOT EXISTS reports (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id      uuid NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
  report      jsonb NOT NULL,                       -- the full CompetitiveReport
  md_export   text,                                 -- cached markdown (filled on first export)
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS competitors (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id     uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  name       text NOT NULL,
  category   text NOT NULL DEFAULT 'direct',        -- direct|indirect
  rationale  text
);
CREATE INDEX IF NOT EXISTS ix_competitors_run ON competitors (run_id);

CREATE TABLE IF NOT EXISTS signals (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id        uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  agent         text NOT NULL,                      -- news|product|review
  competitor    text NOT NULL,
  type          text NOT NULL,                      -- launch|funding|pricing|feature|complaint|sentiment
  payload       jsonb NOT NULL,                     -- the typed item as emitted
  evidence_ids  jsonb NOT NULL DEFAULT '[]',        -- ["ev-9f2c",...]
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_signals_run ON signals (run_id);

CREATE TABLE IF NOT EXISTS evidence (
  id           text PRIMARY KEY,                    -- "ev-" + uuid4 hex (code-generated)
  run_id       uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  claim_ref    text NOT NULL,                       -- "rec:bundle-ai" / "pricing:coda"
  source_type  text NOT NULL,                       -- news|pricing|review|web|document
  source_name  text NOT NULL,
  url          text NOT NULL,
  snippet      text NOT NULL,                       -- ≤280 chars, enforced in code
  source_date  text,                                -- as found; display-only
  agent        text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_evidence_run_claim ON evidence (run_id, claim_ref);

CREATE TABLE IF NOT EXISTS search_cache (
  key         text PRIMARY KEY,                     -- sha256(normalized query)[:16]
  value       jsonb NOT NULL,                       -- [{title,url,content}]
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- RAG bookkeeping (Plan A File Search) — powers the 10-store LRU rule + /documents listing
CREATE TABLE IF NOT EXISTS kb_stores (
  company_slug  text PRIMARY KEY,
  store_name    text NOT NULL,                      -- provider-side store id
  is_hero       boolean NOT NULL DEFAULT false,     -- heroes never LRU-deleted
  last_used_at  timestamptz NOT NULL DEFAULT now(),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS documents (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_slug  text NOT NULL REFERENCES kb_stores(company_slug) ON DELETE CASCADE,
  filename      text NOT NULL,
  status        text NOT NULL DEFAULT 'indexed',    -- indexed|failed
  chunks        int,
  uploaded_at   timestamptz NOT NULL DEFAULT now()
);

-- PLAN B ONLY (uncomment if Friday's gate says pgvector):
-- CREATE EXTENSION IF NOT EXISTS vector;
-- CREATE TABLE IF NOT EXISTS embeddings (
--   id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
--   company_slug text NOT NULL, chunk text NOT NULL,
--   embedding vector(384) NOT NULL, meta jsonb NOT NULL DEFAULT '{}');
-- CREATE INDEX IF NOT EXISTS ix_emb_company ON embeddings (company_slug);
-- CREATE INDEX IF NOT EXISTS ix_emb_vec ON embeddings USING ivfflat (embedding vector_cosine_ops);
