"""Postgres connection pool (psycopg 3).

Lazily builds one pool from the environment. Two ways to point at a database,
checked in order:
  1. DATABASE_URL  — a full postgresql:// DSN (how the app is configured in prod)
  2. PG* env vars  — PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT, read by libpq
                     directly (how you're connecting from the shell right now)

A database is REQUIRED — auth (users + refresh tokens) persists only to
Postgres, with no in-memory fallback. If neither DATABASE_URL nor PG* is set,
pool() raises. is_enabled() is exposed so tests can skip cleanly when no DB is
configured rather than erroring.

This is a thin connection helper, NOT the repository. Dharvi's
app/db/repository.py owns the runs/reports/evidence function set; this module
only provides the pool that auth (and later the repository) borrow connections
from. SSL is forced on — Azure Flexible Server rejects non-TLS connections.
"""
import os
from typing import Optional

from psycopg_pool import ConnectionPool

_pool: Optional[ConnectionPool] = None


def _conninfo() -> Optional[str]:
    url = os.getenv("DATABASE_URL")
    if url:
        # honor an explicit sslmode in the URL; otherwise require TLS
        return url if "sslmode=" in url else f"{url}{'&' if '?' in url else '?'}sslmode=require"
    if os.getenv("PGHOST"):
        # empty-ish conninfo: libpq fills host/user/password/db/port from PG* env
        return "sslmode=require"
    return None


def is_enabled() -> bool:
    """True when a database is configured (DATABASE_URL or PG* env). Used by
    tests to skip when no DB is available; the app itself requires one."""
    return _conninfo() is not None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        conninfo = _conninfo()
        if conninfo is None:
            raise RuntimeError("no database configured (set DATABASE_URL or PG* env vars)")
        _pool = ConnectionPool(conninfo, min_size=1, max_size=5, open=True,
                               kwargs={"connect_timeout": 15})
    return _pool


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
