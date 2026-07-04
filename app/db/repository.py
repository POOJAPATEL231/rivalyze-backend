"""Postgres repository — the FROZEN signatures every other module codes against.

Owner: Dharvi. Psycopg 3 + a connection pool, Azure PostgreSQL Flexible Server
(Supabase URL is an identical-code fallback), no ORM. Parameterized queries
ONLY — every value crosses the wire as a placeholder, never string-interpolated.

Each function opens a pooled connection via a `with` block and returns it to
the pool on exit (commit on success, rollback on exception) — callers never
manage connections themselves. JSONB columns round-trip as plain Python
dicts/lists: psycopg decodes jsonb -> dict automatically on the way out; on
the way in we wrap with `Json(...)` and cast `::jsonb` (mirrors the MS SQL
JSON_MODIFY habit of an explicit cast, even though Postgres would infer it).

MS SQL -> Postgres notes are called out per function where behavior differs.
"""
from __future__ import annotations

import re
from typing import Optional

from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from . import connection


def get_pool() -> ConnectionPool:
    """The shared connection pool (delegates to app.db.connection.pool()).

    One pool per process, not one per module: auth/user_store already borrows
    connections from connection.pool(), which also forces `sslmode=require`
    for Azure Flexible Server. Kept as its own function (rather than every
    call site importing connection directly) so the frozen `get_pool()` name
    other modules already code against still resolves.
    """
    return connection.pool()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "company"


# ============================== companies ===============================
def create_company(name: str, domain: str = "") -> str:
    """Upsert on slug; returns the company id (existing row's id if it already exists).

    MS SQL note: `ON CONFLICT ... DO UPDATE` is Postgres's single-statement
    MERGE — no separate existence check needed.
    """
    sql = """
        INSERT INTO companies (name, slug, domain)
        VALUES (%s, %s, %s)
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, domain = EXCLUDED.domain
        RETURNING id::text
    """
    slug = _slugify(name)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (name, slug, domain or None))
            return cur.fetchone()[0]


# ================================= runs ==================================
def create_run(job_id: str, company_id: str) -> str:
    """Insert the run row; returns the new run id.

    Takes a company_id, not a name — callers that only have a company name
    (e.g. lifecycle.start_run) call create_company(name, domain) first and
    pass its id here. Two small calls instead of a combined one so create_run
    never silently mutates the companies table.
    """
    sql = "INSERT INTO runs (job_id, company_id) VALUES (%s, %s) RETURNING id::text"
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (job_id, company_id))
            return cur.fetchone()[0]


def update_run_status(job_id: str, status: str, stage: str) -> None:
    """Set status + current_stage — called on every pipeline transition."""
    sql = "UPDATE runs SET status = %s, current_stage = %s WHERE job_id = %s"
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (status, stage, job_id))


def append_events(job_id: str, events: list[dict]) -> None:
    """Append to the events array in ONE UPDATE — no read-modify-write race.

    MS SQL note: `JSON_MODIFY` mutates one element at a time; Postgres's
    `jsonb || jsonb` concatenates two arrays natively, so a whole batch of
    events appends atomically in a single round trip.
    """
    sql = "UPDATE runs SET events = events || %s::jsonb WHERE job_id = %s"
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (Json(events), job_id))


def set_lane_stats(job_id: str, stats: dict) -> None:
    """Overwrite lane_stats — called once at finish, not incrementally."""
    sql = "UPDATE runs SET lane_stats = %s::jsonb WHERE job_id = %s"
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (Json(stats), job_id))


def finish_run(job_id: str, threat: Optional[str] = None, confidence: Optional[float] = None) -> None:
    """Mark the run completed, optionally with its threat_level + report_confidence.

    threat/confidence default to None so the current discovery-only vertical
    slice can call `finish_run(job_id)` before the strategist agent exists to
    compute them; the full pipeline calls it with both once it lands — same
    signature either way.
    """
    sql = """
        UPDATE runs
        SET status = 'completed', current_stage = 'done',
            threat_level = %s, report_confidence = %s, finished_at = now()
        WHERE job_id = %s
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (threat, confidence, job_id))


def fail_run(job_id: str, error: str) -> None:
    """Mark the run failed with a one-line, user-safe error (schema: runs.error).

    Not in the original frozen list — added to close a real gap: finish_run()
    only ever recorded a SUCCESSFUL run, so a pipeline exception had no row to
    land on and status='failed' was never actually written anywhere.
    """
    sql = "UPDATE runs SET status = 'failed', error = %s, finished_at = now() WHERE job_id = %s"
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (error, job_id))


def get_run(job_id: str) -> Optional[dict]:
    """Fetch the full run row (the poller's only read)."""
    sql = """
        SELECT id::text, job_id, company_id::text, status, current_stage,
               threat_level, report_confidence, error, events, lane_stats,
               started_at, finished_at
        FROM runs WHERE job_id = %s
    """
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (job_id,))
            return cur.fetchone()


# ================================ reports ================================
def save_report(run_id: str, report: dict, md: str | None = None) -> str:
    """Upsert the report for a run (UNIQUE run_id = 1:1); returns the report id.

    MS SQL note: `ON CONFLICT (run_id)` replaces a MERGE on the unique key —
    a re-run of the same job overwrites its prior report cleanly.
    """
    sql = """
        INSERT INTO reports (run_id, report, md_export)
        VALUES (%s, %s, %s)
        ON CONFLICT (run_id) DO UPDATE SET report = EXCLUDED.report, md_export = EXCLUDED.md_export
        RETURNING id::text
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (run_id, Json(report), md))
            return cur.fetchone()[0]


def get_report(run_id: str) -> Optional[dict]:
    """Fetch the report row (report jsonb decodes to a plain dict)."""
    sql = "SELECT id::text, run_id::text, report, md_export, created_at FROM reports WHERE run_id = %s"
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (run_id,))
            return cur.fetchone()


# ============================== competitors ==============================
def save_competitors(run_id: str, rows: list[dict]) -> None:
    """Bulk-insert discovery's confirmed rival list for a run."""
    sql = "INSERT INTO competitors (run_id, name, category, rationale) VALUES (%s, %s, %s, %s)"
    params = [(run_id, r["name"], r.get("category", "direct"), r.get("rationale", "")) for r in rows]
    if not params:
        return
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, params)


def get_competitors(run_id: str) -> list[dict]:
    """Fetch a run's confirmed rival list — feeds RunStatus.result assembly
    and the Discovery view.

    Not in the original frozen list — added so get_run() can stay a flat row
    (repository stays DB-shape-only) while callers like lifecycle.py join
    competitors themselves to build the typed CompetitorSet.
    """
    sql = "SELECT name, category, rationale FROM competitors WHERE run_id = %s ORDER BY id"
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (run_id,))
            return cur.fetchall()


# ================================ signals =================================
def save_signal(sig: dict) -> str:
    """Insert one typed agent finding; returns the new signal id."""
    sql = """
        INSERT INTO signals (run_id, agent, competitor, type, payload, evidence_ids)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
        RETURNING id::text
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                sig["run_id"], sig["agent"], sig["competitor"], sig["type"],
                Json(sig["payload"]), Json(sig.get("evidence_ids", [])),
            ))
            return cur.fetchone()[0]


# ================================ evidence =================================
def save_evidence(row: dict) -> None:
    """Insert one evidence row. id is code-generated ("ev-" + hex) upstream.

    MS SQL note: `ON CONFLICT (id) DO NOTHING` makes a retried write idempotent
    instead of raising a PK-violation like MS SQL's default INSERT would.
    """
    sql = """
        INSERT INTO evidence (id, run_id, claim_ref, source_type, source_name,
                               url, snippet, source_date, agent)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                row["id"], row["run_id"], row["claim_ref"], row["source_type"],
                row["source_name"], row["url"], row["snippet"],
                row.get("source_date", ""), row["agent"],
            ))


def get_evidence(run_id: str, claim_ref: str) -> list[dict]:
    """Fetch evidence rows for one claim — serves the UI's citation drawer.

    Uses the (run_id, claim_ref) composite index; single-digit-ms lookup.
    """
    sql = """
        SELECT id, run_id::text, claim_ref, source_type, source_name, url,
               snippet, source_date, agent, created_at
        FROM evidence WHERE run_id = %s AND claim_ref = %s
        ORDER BY created_at
    """
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (run_id, claim_ref))
            return cur.fetchall()


def get_evidence_by_ids(evidence_ids: list[str]) -> list[dict]:
    """Fetch evidence rows by their code-generated ids, in the SAME order as
    `evidence_ids` — backs report_to_markdown's evidence_lookup callable.

    Not in the original frozen list (that one is claim_ref-based); added
    because the export builder resolves an Opportunity/Recommendation's
    `evidence_ids` array directly, not a claim_ref.

    MS SQL note: `= ANY(%s)` is Postgres's array-membership test; psycopg
    adapts a Python list straight to a text[] parameter, no IN (...) string
    building needed.
    """
    if not evidence_ids:
        return []
    sql = """
        SELECT id, run_id::text, claim_ref, source_type, source_name, url,
               snippet, source_date, agent, created_at
        FROM evidence WHERE id = ANY(%s)
    """
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (evidence_ids,))
            by_id = {row["id"]: row for row in cur.fetchall()}
    return [by_id[eid] for eid in evidence_ids if eid in by_id]


# ============================ persistence-first ============================
def find_completed_report(company_name: str) -> Optional[dict]:
    """Case-insensitive company match -> most recent completed run, or None.

    Backs the "already analyzed this?" short-circuit in POST /analyze.
    MS SQL note: `lower(name) = lower(%s)` is our ILIKE-equivalent exact match;
    it hits the `companies(lower(name))` unique index, so this is a single
    index seek plus a `runs(company_id, status)` seek — well under 50ms.
    """
    sql = """
        SELECT r.job_id, r.id::text AS run_id
        FROM runs r
        JOIN companies c ON c.id = r.company_id
        WHERE lower(c.name) = lower(%s) AND r.status = 'completed'
        ORDER BY r.finished_at DESC NULLS LAST
        LIMIT 1
    """
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (company_name,))
            return cur.fetchone()


_HISTORY_BASE = """
    SELECT r.job_id, c.name AS company, r.threat_level,
           r.report_confidence AS confidence,
           COALESCE(r.finished_at, r.started_at) AS created_at
    FROM runs r
    JOIN companies c ON c.id = r.company_id
    WHERE r.status = 'completed'
"""
_HISTORY_FILTERED = _HISTORY_BASE + """
    AND c.name ILIKE %s
    ORDER BY COALESCE(r.finished_at, r.started_at) DESC
    LIMIT %s
"""
_HISTORY_UNFILTERED = _HISTORY_BASE + """
    ORDER BY COALESCE(r.finished_at, r.started_at) DESC
    LIMIT %s
"""


def get_history(limit: int = 20, company: str | None = None) -> list[dict]:
    """Completed runs, newest first, optionally filtered by company (ILIKE substring).

    Two fixed, fully-static queries (filtered/unfiltered) instead of building
    the WHERE clause dynamically — no string formatting anywhere near SQL text,
    only `%s` placeholders, so this reads clean under static SAST scanning too.

    MS SQL note: ILIKE is Postgres's case-insensitive LIKE — no COLLATE dance needed.
    """
    if company:
        sql, params = _HISTORY_FILTERED, (f"%{company}%", limit)
    else:
        sql, params = _HISTORY_UNFILTERED, (limit,)
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


# ============================== search cache ==============================
def save_search_cache(key: str, value: dict) -> None:
    """Write-through Postgres half of the cache (Redis is the hot layer)."""
    sql = """
        INSERT INTO search_cache (key, value) VALUES (%s, %s::jsonb)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (key, Json(value)))


def get_search_cache(key: str) -> Optional[dict]:
    """Postgres-side cache read — the fallback when Redis misses or is down."""
    sql = "SELECT value FROM search_cache WHERE key = %s"
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (key,))
            row = cur.fetchone()
            return row[0] if row else None
