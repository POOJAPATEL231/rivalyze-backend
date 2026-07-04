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

import functools
import re
import uuid as _uuid
from datetime import datetime as _datetime, timezone as _tz
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


# =========================== in-memory fallback ============================
# When NO database is configured (connection.is_enabled() is False — local dev,
# MOCK_MODE, offline, CI), the run lifecycle lives in these dicts instead of
# Postgres, so the app is fully exercisable with zero infrastructure. Each
# public function below is decorated with @_fallback("<method>"); the SQL body
# is untouched and only runs when a real DB IS configured. Behaviour and return
# shapes match the SQL paths.
class _MemStore:
    def __init__(self) -> None:
        self.companies: dict = {}    # slug -> {id,name,slug,domain}
        self.runs: dict = {}         # job_id -> run row dict
        self.competitors: dict = {}  # run_id -> [ {name,category,rationale} ]
        self.reports: dict = {}      # run_id -> {report, md_export}
        self.evidence: list = []     # evidence rows
        self.signals: list = []      # signal rows

    def _company_name(self, company_id: str) -> str:
        return (self._company(company_id) or {}).get("name", "")

    def _company(self, company_id: str):
        for c in self.companies.values():
            if c["id"] == company_id:
                return c
        return None

    def _run_by_id(self, run_id: str):
        for r in self.runs.values():
            if r["id"] == run_id:
                return r
        return None

    def create_company(self, name: str, domain: str = "") -> str:
        slug = _slugify(name)
        rec = self.companies.get(slug)
        if rec is None:
            rec = {"id": _uuid.uuid4().hex, "name": name, "slug": slug, "domain": domain or None}
            self.companies[slug] = rec
        else:
            rec["name"], rec["domain"] = name, (domain or rec["domain"])
        return rec["id"]

    def create_run(self, job_id: str, company_id: str) -> str:
        rid = _uuid.uuid4().hex
        self.runs[job_id] = {
            "id": rid, "job_id": job_id, "company_id": company_id,
            "status": "queued", "current_stage": "queued",
            "threat_level": None, "report_confidence": None, "error": None,
            "events": [], "lane_stats": {},
            "started_at": _datetime.now(_tz.utc), "finished_at": None,
        }
        return rid

    def update_run_status(self, job_id: str, status: str, stage: str) -> None:
        r = self.runs.get(job_id)
        if r:
            r["status"], r["current_stage"] = status, stage

    def append_events(self, job_id: str, events: list[dict]) -> None:
        r = self.runs.get(job_id)
        if r:
            r["events"].extend(events)

    def set_lane_stats(self, job_id: str, stats: dict) -> None:
        r = self.runs.get(job_id)
        if r:
            r["lane_stats"] = stats

    def finish_run(self, job_id: str, threat=None, confidence=None) -> None:
        r = self.runs.get(job_id)
        if r:
            r.update(status="completed", current_stage="done", threat_level=threat,
                     report_confidence=confidence, finished_at=_datetime.now(_tz.utc))

    def fail_run(self, job_id: str, error: str) -> None:
        r = self.runs.get(job_id)
        if r:
            r.update(status="failed", error=error, finished_at=_datetime.now(_tz.utc))

    def get_run(self, job_id: str):
        r = self.runs.get(job_id)
        return dict(r) if r else None

    def save_competitors(self, run_id: str, rows: list[dict]) -> None:
        if rows:
            self.competitors.setdefault(run_id, []).extend(
                {"name": x["name"], "category": x.get("category", "direct"),
                 "rationale": x.get("rationale", "")} for x in rows)

    def get_competitors(self, run_id: str) -> list[dict]:
        return list(self.competitors.get(run_id, []))

    def save_report(self, run_id: str, report: dict, md=None) -> str:
        self.reports[run_id] = {"report": report, "md_export": md}
        return _uuid.uuid4().hex

    def get_report(self, run_id: str):
        rec = self.reports.get(run_id)
        return {"id": run_id, "run_id": run_id, "report": rec["report"],
                "md_export": rec["md_export"], "created_at": None} if rec else None

    def find_completed_report(self, company_name: str):
        for r in reversed(list(self.runs.values())):
            if r["status"] == "completed" and \
                    self._company_name(r["company_id"]).lower() == company_name.lower():
                return {"job_id": r["job_id"], "run_id": r["id"]}
        return None

    def get_history(self, limit: int = 20, company: str | None = None) -> list[dict]:
        out: list[dict] = []
        for r in reversed(list(self.runs.values())):
            if r["status"] != "completed":
                continue
            name = self._company_name(r["company_id"])
            if company and company.lower() not in name.lower():
                continue
            out.append({"job_id": r["job_id"], "company": name,
                        "threat_level": r["threat_level"], "confidence": r["report_confidence"],
                        "created_at": r["finished_at"] or r["started_at"]})
            if len(out) >= limit:
                break
        return out

    # ---- two-phase (confirm + analysis + evidence) ----
    def confirm_run(self, job_id: str):
        r = self.runs.get(job_id)
        if r and r["status"] == "awaiting_confirmation":
            r["status"], r["current_stage"] = "confirmed", "confirmed"
            return r["id"]
        return None

    def get_run_company(self, run_id: str):
        r = self._run_by_id(run_id)
        if not r:
            return None
        c = self._company(r["company_id"]) or {}
        return {"name": c.get("name", ""), "domain": c.get("domain")}

    def run_id_exists(self, run_id: str) -> bool:
        return self._run_by_id(run_id) is not None

    def replace_competitors(self, run_id: str, rows: list[dict]) -> None:
        self.competitors[run_id] = [
            {"name": x["name"], "category": x.get("category", "direct"),
             "rationale": x.get("rationale", "")} for x in rows]

    def save_signal(self, sig: dict) -> str:
        sid = _uuid.uuid4().hex
        self.signals.append({**sig, "id": sid})
        return sid

    def save_evidence(self, row: dict) -> None:
        if not any(e.get("id") == row.get("id") for e in self.evidence):  # ON CONFLICT DO NOTHING
            self.evidence.append(dict(row))

    def get_evidence(self, run_id: str, claim_ref: str) -> list[dict]:
        return [e for e in self.evidence
                if e.get("run_id") == run_id and e.get("claim_ref") == claim_ref]

    def get_evidence_by_ids(self, evidence_ids: list[str]) -> list[dict]:
        by_id = {e["id"]: e for e in self.evidence if "id" in e}
        return [by_id[i] for i in evidence_ids if i in by_id]


_mem = _MemStore()


def _fallback(method: str):
    """Route to the in-memory store when no database is configured; otherwise
    run the decorated SQL function unchanged."""
    def deco(sql_fn):
        @functools.wraps(sql_fn)
        def wrapper(*args, **kwargs):
            if not connection.is_enabled():
                return getattr(_mem, method)(*args, **kwargs)
            return sql_fn(*args, **kwargs)
        return wrapper
    return deco


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


def confirm_run(job_id: str) -> Optional[str]:
    """Atomic compare-and-swap for the /confirm gate.

    Only a run in `awaiting_confirmation` transitions to `confirmed`, and the DB
    serializes concurrent callers so exactly ONE wins. Returns the run id on
    success, or None when the run isn't awaiting confirmation (wrong-state OR a
    second /confirm) — the route maps None to 409, so Phase 2 launches exactly
    once and a double-confirm never double-runs the agents.
    """
    sql = """
        UPDATE runs SET status = 'confirmed', current_stage = 'confirmed'
        WHERE job_id = %s AND status = 'awaiting_confirmation'
        RETURNING id::text
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (job_id,))
            row = cur.fetchone()
            return row[0] if row else None


def run_id_exists(run_id: str) -> bool:
    """True if a run with this id exists — backs the evidence endpoint's 404 gate.

    Compares on id::text so a malformed (non-uuid) run_id returns False instead
    of raising, keeping the route on the 404 path rather than a 500.
    """
    sql = "SELECT 1 FROM runs WHERE id::text = %s"
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (run_id,))
            return cur.fetchone() is not None


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


def replace_competitors(run_id: str, rows: list[dict]) -> None:
    """Overwrite a run's competitor list with the user-confirmed set (/confirm).

    Discovery's proposal is deleted first so Phase 2 analyzes EXACTLY what the
    user approved — no leftover rivals the user removed. Delete + insert run in
    one transaction (single pooled connection).
    """
    ins = "INSERT INTO competitors (run_id, name, category, rationale) VALUES (%s, %s, %s, %s)"
    params = [(run_id, r["name"], r.get("category", "direct"), r.get("rationale", "")) for r in rows]
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM competitors WHERE run_id = %s", (run_id,))
            if params:
                cur.executemany(ins, params)


def get_run_company(run_id: str) -> Optional[dict]:
    """Return {name, domain} for a run's company — Phase 2 needs the company name
    (not carried on RunStatus) to frame agent prompts and stamp the report."""
    sql = """
        SELECT c.name, c.domain
        FROM runs r JOIN companies c ON c.id = r.company_id
        WHERE r.id::text = %s
    """
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (run_id,))
            return cur.fetchone()


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


# ---------------------------------------------------------------------------
# Wire the in-memory fallback onto the run-lifecycle + two-phase functions. Each
# keeps its real SQL body (used when a DB is configured); when
# connection.is_enabled() is False the call routes to the matching _MemStore
# method instead, so the WHOLE app — both phases, /confirm, and the evidence
# drawer — runs locally / in MOCK with no Postgres. search_cache is left DB-only
# on purpose: cache.py already degrades gracefully when it's absent.
for _fn_name in ("create_company", "create_run", "update_run_status", "append_events",
                 "set_lane_stats", "finish_run", "fail_run", "get_run", "save_competitors",
                 "get_competitors", "save_report", "get_report", "find_completed_report",
                 "get_history", "confirm_run", "get_run_company", "run_id_exists",
                 "replace_competitors", "save_signal", "save_evidence", "get_evidence",
                 "get_evidence_by_ids"):
    globals()[_fn_name] = _fallback(_fn_name)(globals()[_fn_name])
