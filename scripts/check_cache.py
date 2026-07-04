"""Cache connectivity probe — answers "do we ACTUALLY have a working cache?"

Run it in the SAME environment the app runs in (locally with prod env vars
exported, or in the Azure App Service SSH/console) so it reads the real
REDIS_URL / DATABASE_URL:

    python scripts/check_cache.py

It checks, in order:
  1. REDIS_URL present + has a valid scheme (rediss:// / redis://) — a bare
     host:port silently disables Redis (the PR #20 trap).
  2. A live Redis PING.
  3. DATABASE_URL/PG present + the search_cache table exists (the write-through
     fallback; if it's missing the cache silently no-ops even with Postgres).
  4. A real cache_set -> cache_get round trip through the app's own cache module.

Exit code 0 = at least one working cache layer; 1 = no cache is active (every
search will hit the paid API on every run).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK, WARN, BAD = "[ OK ]", "[WARN]", "[FAIL]"
redis_ok = pg_ok = False


def line(tag, msg):
    print(f"{tag} {msg}")


# ---------------------------------------------------------------- 1 & 2: Redis
redis_url = os.getenv("REDIS_URL", "")
if not redis_url:
    line(WARN, "REDIS_URL is NOT set -> Redis hot layer disabled (Postgres/miss only)")
elif not redis_url.startswith(("redis://", "rediss://", "unix://")):
    line(BAD, f"REDIS_URL has NO scheme ({redis_url[:20]}...) -> silently disabled. "
              "Azure needs rediss://:<key>@<name>.redis.cache.windows.net:6380/0")
else:
    scheme = redis_url.split("://", 1)[0]
    tls = " (TLS)" if scheme == "rediss" else " (NO TLS — Azure expects rediss:// on 6380)"
    line(OK, f"REDIS_URL scheme = {scheme}://{tls}")
    try:
        import redis
        client = redis.Redis.from_url(redis_url, socket_timeout=5, decode_responses=True)
        pong = client.ping()
        line(OK, f"Redis PING -> {pong}") if pong else line(BAD, "Redis PING returned falsy")
        redis_ok = bool(pong)
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"Redis connect FAILED: {type(exc).__name__}: {exc}")

# ------------------------------------------------------- 3: Postgres + table
if os.getenv("DATABASE_URL") or os.getenv("PGHOST"):
    try:
        from app.db import repository
        with repository.get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.search_cache')")
                exists = cur.fetchone()[0]
        if exists:
            line(OK, "Postgres search_cache table exists -> write-through fallback active")
            pg_ok = True
        else:
            line(BAD, "Postgres reachable but search_cache table is MISSING -> "
                      "cache no-ops. Run app/db/schema.sql to create it.")
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"Postgres check FAILED: {type(exc).__name__}: {exc}")
else:
    line(WARN, "No DATABASE_URL/PGHOST -> Postgres write-through disabled")

# ------------------------------------------------- 4: real round trip via cache.py
try:
    from app.core import cache
    key = cache.make_cache_key("__cache_probe__")
    cache.cache_set(key, {"probe": "value"})
    got = cache.cache_get(key)
    if got and got.get("probe") == "value":
        line(OK, "Round trip through cache.py: set -> get SUCCEEDED (a repeat search WILL hit cache)")
    else:
        line(BAD, f"Round trip returned {got!r} -> a repeat search will NOT be cached")
except Exception as exc:  # noqa: BLE001
    line(BAD, f"cache.py round trip errored: {type(exc).__name__}: {exc}")

print("-" * 60)
if redis_ok:
    print("VERDICT: Redis cache is ACTIVE (hot layer). ✔")
elif pg_ok:
    print("VERDICT: No Redis, but Postgres write-through cache is ACTIVE. ✔")
    print("         (Add REDIS_URL for the faster hot layer.)")
else:
    print("VERDICT: NO working cache - every search hits the paid API on every run. [x]")
sys.exit(0 if (redis_ok or pg_ok) else 1)
