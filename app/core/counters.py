"""
app/core/counters.py

WHAT THIS MODULE IS
--------------------
A tiny wrapper around Redis that keeps a running count of how many times
each search/LLM provider has been called *today*. Think of it as a
scoreboard: "Tavily has been called 47 times today", "Serper 12 times",
"Groq (LLM) 200 times", etc.

WHY IT EXISTS
-------------
- Tavily/Serper are PAID APIs with daily quotas. If we blow through the
  quota mid-demo, search silently starts failing. Having a live counter
  means Mihir's LLM router (and we, on Sunday morning) can see usage and
  react before that happens.
- Dharvi's warmup script and the Sunday credit_report.py script both read
  these counters to print a usage summary for the team.

KEY NAMING SCHEME
------------------
Keys are namespaced by provider + calendar date, e.g.:
    "credits:tavily:2026-07-04"
    "credits:serper:2026-07-04"
    "llm:groq:2026-07-04"
Because the date is baked into the key, counts automatically "reset" every
day just by virtue of a new key existing — we never need a cron job or
manual reset logic.

THE ONE RULE THAT MATTERS MOST
-------------------------------
Counters must NEVER be able to break a real user-facing run. Redis is an
optional convenience here, not a dependency the rest of the system can
lean on. So every public function in this file follows the same contract:

    - If Redis is unreachable, unset, or errors out in any way:
        -> log a warning (so we can see it in ops), and
        -> return 0
    - NEVER raise an exception out of this module.
    - NEVER retry / block / sleep waiting for Redis to come back — that
      would slow down or hang a live analysis run over something that is
      purely cosmetic/telemetry.

This means every caller elsewhere in the codebase can call counter_incr()
or counter_get() without wrapping it in a try/except of their own.

Owner: Tushar
"""

import os
import logging
import datetime
import time

# Module-level logger. Using __name__ means log lines will show up as
# "app.core.counters" in the log output, so anyone scanning logs during
# the demo can immediately tell where a warning came from.
logger = logging.getLogger(__name__)

# Module-level cache of the Redis client object.
#
# Why a module-level global instead of creating a new client per call?
# Creating a redis.Redis / ConnectionPool object is relatively cheap, but
# calling .ping() on it (which we do below, to verify the connection is
# actually alive) does a real network round-trip. Since search() will call
# counter_incr() on almost every single web request across a run, we do
# NOT want to pay that round-trip cost every single time. So we connect
# once, cache the client here, and reuse it for the lifetime of the
# process.
#
# Trade-off: if Redis goes down mid-process and comes back up, this cached
# client *does* keep working fine (redis-py's client re-establishes TCP
# connections under the hood as needed) — the only case we don't recover
# from automatically is "Redis was down at the moment we first tried to
# connect", in which case _redis_client stays None for the rest of the
# process. That's an acceptable trade for a hackathon: we are not writing
# a reconnect-with-backoff loop for a scoreboard.
_redis_client = None

# Negative cache for connection failures. Without this, a SET-BUT-
# UNREACHABLE REDIS_URL (valid scheme, wrong/down host — a common
# transient) makes _redis_client stay None forever, so EVERY call re-runs
# redis.from_url(...).ping() and eats the full socket_timeout (~3s) again.
# Anything that calls counter_get() in a loop over several providers (e.g.
# a health/credits endpoint) turns into a multi-second stall per request
# right when Redis is already degraded — exactly the wrong time for an
# endpoint to get slow. Caching "we just failed" for a cooldown window
# turns repeat failures into an instant no-op instead of a fresh network
# round trip each time.
_last_connect_failure_at: float | None = None
_RECONNECT_COOLDOWN_S = 30


def _get_client():
    """
    Lazily create (and cache) the Redis client.

    Returns the shared redis.Redis instance, or None if we could not
    connect (missing REDIS_URL, package not installed, network error,
    wrong scheme, bad auth, etc). Callers must treat None as "counters are
    unavailable right now" and fall back to returning 0 — they must NOT
    propagate an exception.
    """
    global _redis_client, _last_connect_failure_at

    # Fast path: we already have a working client from a previous call in
    # this process. Reuse it instead of reconnecting.
    if _redis_client is not None:
        return _redis_client

    # Negative-cache fast path: a connection attempt failed recently, so
    # don't pay another ~3s timeout just to almost certainly fail again.
    if (_last_connect_failure_at is not None
            and time.monotonic() - _last_connect_failure_at < _RECONNECT_COOLDOWN_S):
        return None

    url = os.getenv("REDIS_URL")
    if not url:
        # No Redis configured at all (e.g. local dev without Azure Redis
        # wired up yet). This is expected during early development, so we
        # warn (not error) and let every counter call quietly return 0.
        logger.warning("counters: REDIS_URL not set — all counter calls return 0")
        return None

    try:
        # Imported inside the function (not at module top) on purpose:
        # if the `redis` package somehow isn't installed in some
        # environment, importing it at module load time would crash the
        # entire app on startup. Importing it lazily here means that
        # failure is contained to "counters don't work", not "the whole
        # backend won't boot".
        import redis

        # decode_responses=True: makes redis-py return `str` instead of
        # `bytes` for everything, so counter_get() doesn't have to decode
        # manually.
        #
        # socket_timeout=3: caps how long we'll wait on a single Redis
        # operation. Without this, a network blip could make a counter
        # call hang for the OS-level TCP timeout (which can be 30s+),
        # and since counters are called from the hot path of search(),
        # that would visibly slow down (or appear to hang) a live run.
        #
        # IMPORTANT GOTCHA (see project docs): Azure Cache for Redis in
        # TLS mode uses the "rediss://" scheme — note the DOUBLE "s"
        # (redis + s for secure), and port 6380, e.g.:
        #   rediss://:<KEY>@<NAME>.redis.cache.windows.net:6380/0
        # A single "s" ("redis://") is the plaintext scheme on port 6379
        # and Azure will simply refuse that connection. redis.from_url()
        # picks TLS on/off purely based on which scheme string is in the
        # URL, so this is a pure copy-paste risk when setting the env var
        # — not something this code can detect or fix for you.
        _redis_client = redis.from_url(url, decode_responses=True, socket_timeout=3)

        # .ping() forces an actual connection attempt right now, rather
        # than lazily on the first real command. We want to know
        # immediately (and log once) if the URL/credentials are bad,
        # rather than discovering it silently deep inside counter_incr().
        _redis_client.ping()
        _last_connect_failure_at = None  # clear the negative cache on a good connection
        return _redis_client

    except Exception as e:
        # Deliberately broad `except Exception`: this can be a missing
        # package (ImportError), a DNS failure, a TLS handshake failure,
        # a wrong password (AuthenticationError), a timeout, etc. We do
        # not care WHICH failure it is for control-flow purposes — every
        # single one of them means "treat counters as unavailable" per
        # the golden rule at the top of this file.
        logger.warning("counters: Redis connection failed (%s) — returning 0 for %ds", e, _RECONNECT_COOLDOWN_S)

        # Leave _redis_client as None (not a broken/half-open client) so
        # that the NEXT call to _get_client() cleanly retries a fresh
        # connection attempt instead of reusing something broken. Record
        # the failure time so THAT retry doesn't happen until the cooldown
        # elapses (see _last_connect_failure_at above).
        _redis_client = None
        _last_connect_failure_at = time.monotonic()
        return None


def counter_incr(name: str) -> int:
    """
    Atomically increment the counter stored at Redis key `name` by 1 and
    return the new total.

    `name` is expected to be a fully-formed key, typically produced by
    today_key(provider) — e.g. counter_incr(today_key("tavily")) rather
    than counter_incr("tavily") directly.

    Returns 0 (never raises) if Redis is unavailable or the operation
    fails for any reason. Per the golden rule, a broken counter must never
    stop a real search/LLM call from happening — so every caller should
    treat this return value as "best effort telemetry", not as something
    to branch business logic on.
    """
    client = _get_client()
    if client is None:
        return 0
    try:
        # redis INCR is atomic server-side, so concurrent calls (e.g. two
        # agents both hitting Tavily in the same parallel pipeline stage)
        # cannot race and undercount each other. It also auto-creates the
        # key starting at 0 if it doesn't exist yet (i.e. first call of
        # the day for that provider naturally returns 1).
        return int(client.incr(name))
    except Exception as e:
        # Same reasoning as _get_client(): any failure here (connection
        # dropped between ping() and incr(), timeout, etc.) is logged and
        # swallowed, never raised.
        logger.warning("counters: incr(%s) failed: %s", name, e)
        return 0


def counter_get(name: str) -> int:
    """
    Read the current value of counter `name` without modifying it.

    Used by:
      - Mihir's LLM router, to check whether a provider has already hit
        its daily budget before deciding whether to route to it.
      - Dharvi's warmup script / the Sunday credit_report.py script, to
        print a usage summary for the team.

    Returns 0 (never raises) if Redis is unavailable, or if the key does
    not exist yet (i.e. that provider hasn't been called today).
    """
    client = _get_client()
    if client is None:
        return 0
    try:
        val = client.get(name)
        # A brand-new key (nothing incremented yet today) comes back as
        # None from redis-py, not "0" — we normalize that to the integer
        # 0 so callers never have to special-case None vs "0" themselves.
        return int(val) if val is not None else 0
    except Exception as e:
        logger.warning("counters: get(%s) failed: %s", name, e)
        return 0


def today_key(provider: str) -> str:
    """
    Build the standard counter key for `provider` for *today's* date, in
    the "credits:<provider>:<YYYY-MM-DD>" format described at the top of
    this file.

    Usage pattern (from search_chain.py, for example):
        counter_incr(today_key("tavily"))
        counter_get(today_key("tavily"))

    Because the date is embedded in the key itself, counts naturally
    "reset" at midnight — there is no cleanup job, cron, or TTL needed.
    datetime.date.today() is evaluated fresh on every call (not cached),
    so a process that stays running across midnight will correctly start
    writing to a new key for the new day without needing a restart.
    """
    return f"credits:{provider}:{datetime.date.today().isoformat()}"
