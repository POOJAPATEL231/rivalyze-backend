"""Database repository — STUB (Dharvi's full module will replace this file).

Owner: Dharvi (real) · Consumer: app/core/cache.py (Phase 1 — Mihir)
This stub exists so the cache module can import the FROZEN interface:
    get_search_cache(key: str) -> dict | None
    save_search_cache(key: str, value: dict) -> None

Why a shim, not inlined SQL in cache.py:
    The execution plan (§"Repository functions consumed (Dharvi's — do not
    redefine)") explicitly carves these two functions out as Dharvi's
    territory. Keeping them behind a stable import boundary lets Dharvi
    ship the full module (parameterised queries, retry policy, pooling)
    without cache.py ever needing to change.

Contract:
    - Both functions never raise. A cache miss on the DB side is None
      (not an exception). A write failure is swallowed at the call site
      (cache module emits one event).
    - The connection pool is lazy: nothing is opened until the first call.
      This lets the test suite and MOCK_MODE boot without DATABASE_URL set.
    - All SQL is parameterised (`%s` placeholders) per the data dictionary's
      "parameterised queries only" rule.
"""

from __future__ import annotations

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)


# --- connection pool (lazy, module-level) ---
# `psycopg_pool.ConnectionPool` is thread-safe; the cache module is called
# from FastAPI's threadpool, so we share one pool across the process.
#
# We import lazily so that environments without `psycopg` installed (e.g.
# a pure-frontend CI run) can still `import app.db.repository` without
# crashing at module load.
_pool = None
_pool_lock = threading.Lock()
_POOL_TIMEOUT = 5.0  # seconds — never let a cache call block the run


def _get_pool():
    """Return the process-wide pool, opening it on first use.

    Returns None if DATABASE_URL is not set or the pool cannot be opened —
    in which case the cache module's call sites treat it as a miss
    (graceful degradation, never a crash).
    """
    global _pool
    if _pool is not None:
        return _pool

    url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL")
    if not url:
        return None

    # Lazy import keeps the module importable without psycopg installed.
    try:
        from psycopg_pool import ConnectionPool
    except Exception as exc:  # pragma: no cover — exercised only on broken envs
        logger.warning("repository: psycopg_pool unavailable: %s", exc)
        return None

    with _pool_lock:
        if _pool is not None:  # double-checked locking
            return _pool
        try:
            _pool = ConnectionPool(
                conninfo=url,
                min_size=0,         # don't keep idle connections in dev
                max_size=5,         # cap concurrency — cache is hot, not heavy
                timeout=_POOL_TIMEOUT,
                kwargs={"autocommit": True},  # every statement is its own tx
                open=False,         # don't open until first checkout
            )
            _pool.open(wait=False, timeout=_POOL_TIMEOUT)
            logger.info("repository: pool opened")
        except Exception as exc:
            logger.warning("repository: pool open failed (%s) — DB writes disabled", exc)
            _pool = None
    return _pool


# --- public API (FROZEN) ---

def get_search_cache(key: str) -> dict | None:
    """Read a cached value by its 16-char hex key. Returns None on miss or any error.

    Never raises. A DB outage is indistinguishable from a miss to the caller —
    that's the whole point of the cache module's degradation rules.
    """
    pool = _get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM search_cache WHERE key = %s",
                    (key,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        value = row[0]
        # The column is jsonb — psycopg returns a Python object already.
        # If a writer ever stored a raw string, json.loads is the safety net.
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, (bytes, bytearray)):
            return json.loads(value.decode())
        if isinstance(value, str):
            return json.loads(value)
        return value  # last resort — let the caller decide
    except Exception as exc:
        logger.debug("repository: get_search_cache miss (%s): %s", type(exc).__name__, exc)
        return None


def save_search_cache(key: str, value: dict) -> None:
    """Upsert a cached value. Never raises.

    Uses `ON CONFLICT (key) DO UPDATE` so repeated writes (the same query
    hitting search results in 30 minutes) collapse to a single row with
    a fresh `created_at` — that's the only column that changes on conflict.
    """
    pool = _get_pool()
    if pool is None:
        return
    try:
        # jsonb needs a JSON string; psycopg also accepts a Python dict via
        # the Json adapter, but doing it ourselves keeps the shim small.
        payload = json.dumps(value)
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO search_cache (key, value)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        created_at = now()
                    """,
                    (key, payload),
                )
    except Exception as exc:
        logger.debug("repository: save_search_cache fail (%s): %s", type(exc).__name__, exc)
        return None  # explicit — caller shouldn't see an exception either


# --- test seam ---
# A small hook so tests can swap the pool for an in-memory shim without
# touching DATABASE_URL. Not part of the public spec.
def _set_pool_for_tests(p) -> None:  # pragma: no cover — tests only
    global _pool
    _pool = p
