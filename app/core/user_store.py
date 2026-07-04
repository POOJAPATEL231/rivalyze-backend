"""User store for auth. Persists to Postgres when a database is configured
(DATABASE_URL or PG* env), otherwise keeps users in memory.

The in-memory path is what tests and offline/MOCK dev use — it needs no DB.
The Postgres path uses the shared `users` table (schema.sql) via the pool in
app/db/connection.py, ALWAYS with parameterized queries (never string-built
SQL). This mirrors the signatures Dharvi's repository will eventually own, so
auth_routes.py doesn't change regardless of which path is active.

Security invariants (both paths): only bcrypt hashes are stored, never
plaintext; emails are normalized (lower/strip) so one account maps to one
email case-insensitively.
"""
import uuid

from ..db import connection

# --- in-memory fallback state (used only when no database is configured) ---
_USERS: dict[str, dict] = {}      # id -> {"id","email","password_hash"}
_BY_EMAIL: dict[str, str] = {}    # normalized email -> id


def _norm(email: str) -> str:
    return email.lower().strip()


def create_user(email: str, password_hash: str) -> dict:
    email = _norm(email)
    if connection.is_enabled():
        with connection.pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) "
                "RETURNING id::text, email",
                (email, password_hash),
            )
            uid, em = cur.fetchone()
            conn.commit()
            return {"id": uid, "email": em, "password_hash": password_hash}

    uid = uuid.uuid4().hex
    rec = {"id": uid, "email": email, "password_hash": password_hash}
    _USERS[uid] = rec
    _BY_EMAIL[email] = uid
    return rec


def get_user_by_email(email: str) -> dict | None:
    email = _norm(email)
    if connection.is_enabled():
        with connection.pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id::text, email, password_hash FROM users "
                "WHERE lower(email) = lower(%s)",
                (email,),
            )
            row = cur.fetchone()
            return {"id": row[0], "email": row[1], "password_hash": row[2]} if row else None

    uid = _BY_EMAIL.get(email)
    return _USERS.get(uid) if uid else None


def get_user_by_id(user_id: str) -> dict | None:
    if connection.is_enabled():
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

    return _USERS.get(user_id)
