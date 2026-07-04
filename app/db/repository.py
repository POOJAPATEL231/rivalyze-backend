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
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from ..core import config

_pool: Optional[ConnectionPool] = None


def get_pool() -> ConnectionPool:
    """Lazy singleton connection pool (one per process).

    MS SQL note: replaces ADO.NET's implicit connection pooling — here it's
    explicit and shared across every repository call via this one function.
    """
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=10, open=True)
    return _pool


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
    """Insert the run row; returns the new run id."""
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


def finish_run(job_id: str, threat: str, confidence: float) -> None:
    """Mark the run completed with its final threat_level + report_confidence."""
    sql = """
        UPDATE runs
        SET status = 'completed', current_stage = 'done',
            threat_level = %s, report_confidence = %s, finished_at = now()
        WHERE job_id = %s
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (threat, confidence, job_id))


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


def get_history(limit: int = 20, company: str | None = None) -> list[dict]:
    """Completed runs, newest first, optionally filtered by company (ILIKE substring).

    MS SQL note: ILIKE is Postgres's case-insensitive LIKE — no COLLATE dance needed.
    """
    where = "WHERE r.status = 'completed'"
    params: list[Any] = []
    if company:
        where += " AND c.name ILIKE %s"
        params.append(f"%{company}%")
    sql = f"""
        SELECT r.job_id, c.name AS company, r.threat_level,
               r.report_confidence AS confidence,
               COALESCE(r.finished_at, r.started_at) AS created_at
        FROM runs r
        JOIN companies c ON c.id = r.company_id
        {where}
        ORDER BY COALESCE(r.finished_at, r.started_at) DESC
        LIMIT %s
    """
    params.append(limit)
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
