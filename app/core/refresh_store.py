"""Refresh-token store. Postgres when a database is configured, in-memory
otherwise (same pattern as user_store).

Only the SHA-256 HASH of a refresh token is ever stored — the raw token exists
only in the client. Rows are marked `revoked` rather than deleted so that a
presented-but-already-rotated token can be detected as reuse (possible theft)
and trigger revoke_all_for_user. Parameterized SQL only.
"""
import datetime as dt
import uuid

from ..db import connection

# in-memory fallback: token_hash -> record
_TOKENS: dict[str, dict] = {}


def store(user_id: str, token_hash: str, expires_at: dt.datetime) -> None:
    if connection.is_enabled():
        with connection.pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO refresh_tokens (user_id, token_hash, expires_at) "
                "VALUES (%s::uuid, %s, %s)",
                (user_id, token_hash, expires_at),
            )
            conn.commit()
        return
    _TOKENS[token_hash] = {
        "user_id": user_id, "token_hash": token_hash,
        "expires_at": expires_at, "revoked": False,
    }


def get(token_hash: str) -> dict | None:
    if connection.is_enabled():
        with connection.pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT user_id::text, token_hash, expires_at, revoked "
                "FROM refresh_tokens WHERE token_hash = %s",
                (token_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {"user_id": row[0], "token_hash": row[1],
                    "expires_at": row[2], "revoked": row[3]}
    return _TOKENS.get(token_hash)


def revoke(token_hash: str) -> None:
    if connection.is_enabled():
        with connection.pool().connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE refresh_tokens SET revoked = true WHERE token_hash = %s",
                        (token_hash,))
            conn.commit()
        return
    rec = _TOKENS.get(token_hash)
    if rec:
        rec["revoked"] = True


def revoke_all_for_user(user_id: str) -> None:
    if connection.is_enabled():
        try:
            uuid.UUID(user_id)
        except (ValueError, AttributeError, TypeError):
            return
        with connection.pool().connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE refresh_tokens SET revoked = true WHERE user_id = %s::uuid",
                        (user_id,))
            conn.commit()
        return
    for rec in _TOKENS.values():
        if rec["user_id"] == user_id:
            rec["revoked"] = True
