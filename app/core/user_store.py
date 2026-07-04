"""User store for auth — Postgres only.

Backed by the `users` table (schema.sql) via the pool in app/db/connection.py,
ALWAYS with parameterized queries (never string-built SQL). There is no
in-memory fallback: a database must be configured (DATABASE_URL or PG* env) or
these calls raise — auth data is never held in process memory.

Security invariants: only bcrypt hashes are stored, never plaintext; emails are
normalized (lower/strip) so one account maps to one email case-insensitively.
"""
import uuid

from psycopg import errors

from ..db import connection


class EmailAlreadyExistsError(Exception):
    """Email is already registered. The unique index on lower(email) is the
    source of truth: create_user does an unconditional INSERT and maps the
    resulting UniqueViolation to this, so a concurrent duplicate signup that
    slips past any pre-check surfaces as a clean 409 — never a 500."""


def _norm(email: str) -> str:
    return email.lower().strip()


def create_user(email: str, password_hash: str) -> dict:
    email = _norm(email)
    try:
        with connection.pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) "
                "RETURNING id::text, email",
                (email, password_hash),
            )
            uid, em = cur.fetchone()
            conn.commit()
    except errors.UniqueViolation:
        # the INSERT lost the race (or a pre-check was stale) — constraint wins
        raise EmailAlreadyExistsError(email)
    return {"id": uid, "email": em, "password_hash": password_hash}


def get_user_by_email(email: str) -> dict | None:
    email = _norm(email)
    with connection.pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, email, password_hash FROM users "
            "WHERE lower(email) = lower(%s)",
            (email,),
        )
        row = cur.fetchone()
    return {"id": row[0], "email": row[1], "password_hash": row[2]} if row else None


def get_user_by_id(user_id: str) -> dict | None:
    # guard the ::uuid cast so a malformed subject never raises (→ 401, not 500)
    try:
        uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        return None
    with connection.pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, email, password_hash FROM users WHERE id = %s::uuid",
            (user_id,),
        )
        row = cur.fetchone()
    return {"id": row[0], "email": row[1], "password_hash": row[2]} if row else None
