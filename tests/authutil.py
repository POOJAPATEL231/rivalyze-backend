"""Shared test helper for the user-scoped routes.

Since GET /history and POST /analyze* now identify the caller via
get_current_user (JWT `sub` -> user_id), tests that hit those routes need a real
user plus a bearer header, and runs they seed directly must be stamped with that
user's id or they won't surface in the user-scoped history.

make_user() creates a user straight through user_store (no HTTP/refresh-token
round trip) and mints a matching access token. cleanup_users() removes them by
the email tag on teardown.
"""
import uuid

from app.core import security, user_store
from app.db import connection

_PASSWORD = "s3cretpassword"


def make_user(tag: str | None = None) -> dict:
    """Create a user; return {user_id, email, token, headers}. The token's `sub`
    is the user_id, so passing `headers` to a route is enough to be identified,
    and `user_id` is what create_run must be stamped with."""
    tag = tag or uuid.uuid4().hex[:10]
    email = f"histuser.{tag}@example.com"
    user = user_store.create_user(email, security.hash_password(_PASSWORD))
    token = security.create_access_token(user["id"], email)
    return {
        "user_id": user["id"],
        "email": email,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def cleanup_users(*user_ids: str) -> None:
    """Delete the given users (best-effort) on fixture teardown."""
    ids = [u for u in user_ids if u]
    if not ids:
        return
    with connection.pool().connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = ANY(%s::uuid[])", (ids,))
        conn.commit()
