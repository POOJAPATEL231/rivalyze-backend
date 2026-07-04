"""Postgres repository — runs + competitors persistence.

A MINIMAL subset of the frozen repository signatures (module B13, Dharvi's
domain) covering exactly what today's pipeline needs, so runs survive restarts
and GET /runs reads from Postgres instead of process memory. The API process
owns no run state (restart-safe = TC-P01). Parameterized SQL only.

When Dharvi's canonical repository lands, these bodies get superseded — the
function names here are a subset of hers, so callers won't change.
"""
import json

from .connection import pool
from ..models import Competitor, CompetitorSet, RunEvent, RunStatus


def _slug(name: str) -> str:
    return "-".join(name.lower().strip().split())[:60] or "company"


# ------------------------------ companies ------------------------------
def get_or_create_company(name: str, domain: str = "") -> str:
    slug = _slug(name)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO companies (name, slug, domain) VALUES (%s, %s, %s) "
            "ON CONFLICT (slug) DO UPDATE SET domain = COALESCE(companies.domain, EXCLUDED.domain) "
            "RETURNING id::text",
            (name, slug, domain or None),
        )
        company_id = cur.fetchone()[0]
        conn.commit()
    return company_id


# -------------------------------- runs ---------------------------------
def create_run(job_id: str, company: str, domain: str = "") -> str:
    """Create the company (if new) + a queued run row. Returns the run uuid."""
    company_id = get_or_create_company(company, domain)
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (job_id, company_id, status, current_stage) "
            "VALUES (%s, %s, 'queued', 'queued') RETURNING id::text",
            (job_id, company_id),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def update_run_status(job_id: str, status: str, current_stage: str) -> None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET status = %s, current_stage = %s WHERE job_id = %s",
            (status, current_stage, job_id),
        )
        conn.commit()


def append_events(job_id: str, events: list[dict]) -> None:
    """Append to the ledger with a single jsonb-concat UPDATE — no
    read-modify-write, so concurrent appends never clobber each other."""
    if not events:
        return
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET events = events || %s::jsonb WHERE job_id = %s",
            (json.dumps(events), job_id),
        )
        conn.commit()


def set_lane_stats(job_id: str, lane_stats: dict) -> None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET lane_stats = %s::jsonb WHERE job_id = %s",
            (json.dumps(lane_stats), job_id),
        )
        conn.commit()


def finish_run(job_id: str, status: str, error: str | None = None) -> None:
    with pool().connection() as conn, conn.cursor() as cur:
        if status == "completed":
            cur.execute(
                "UPDATE runs SET status = 'completed', current_stage = 'done', "
                "error = NULL, finished_at = now() WHERE job_id = %s",
                (job_id,),
            )
        else:
            cur.execute(
                "UPDATE runs SET status = %s, error = %s, finished_at = now() "
                "WHERE job_id = %s",
                (status, error, job_id),
            )
        conn.commit()


def save_competitors(run_id: str, rows: list[dict]) -> None:
    if not rows:
        return
    with pool().connection() as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                "INSERT INTO competitors (run_id, name, category, rationale) "
                "VALUES (%s::uuid, %s, %s, %s)",
                (run_id, r.get("name"), r.get("category", "direct"), r.get("rationale", "")),
            )
        conn.commit()


def find_completed_run(company: str) -> str | None:
    """Persistence-first: the most recent completed run for this company
    (case-insensitive by slug), or None."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.job_id FROM runs r JOIN companies c ON c.id = r.company_id "
            "WHERE c.slug = %s AND r.status = 'completed' "
            "ORDER BY r.finished_at DESC NULLS LAST LIMIT 1",
            (_slug(company),),
        )
        row = cur.fetchone()
    return row[0] if row else None


def get_run(job_id: str) -> RunStatus | None:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, job_id, status, current_stage, events, lane_stats, error "
            "FROM runs WHERE job_id = %s",
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        run_id, jid, status, stage, events, lane_stats, error = row
        cur.execute(
            "SELECT name, category, rationale FROM competitors WHERE run_id = %s::uuid ORDER BY id",
            (run_id,),
        )
        comp_rows = cur.fetchall()

    competitors = [Competitor(name=n, category=c, rationale=r or "") for n, c, r in comp_rows]
    return RunStatus(
        job_id=jid,
        status=status,
        current_stage=stage,
        events=[RunEvent(**e) for e in (events or [])],
        result=CompetitorSet(competitors=competitors) if competitors else None,
        lane_stats=lane_stats or {},
        run_id=run_id,
        error=error,
    )
