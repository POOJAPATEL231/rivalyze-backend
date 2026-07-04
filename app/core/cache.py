"""Cache module — redis-py against Azure Cache for Redis with Postgres write-through fallback.

Owner: Mihir
Consumed by: app/core/search_chain.py (Tushar)
Never raises — all failures degrade silently to cache miss + one emitted event.
"""

import hashlib
import json
import logging
import os
from typing import Callable

import redis
import redis.exceptions

logger = logging.getLogger(__name__)

# --- Module-level lazy singleton ---
_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis | None:
    """Return a lazy-initialised Redis client, or None if REDIS_URL is not set."""
    global _redis_client
    if _redis_client is None:
        url = os.getenv("REDIS_URL")
        if url:
            _redis_client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_timeout=3,
            )
    return _redis_client


def make_cache_key(query: str) -> str:
    """Normalise a search query and produce a 16-char hex cache key.

    sha256(query.lower().strip())[:16] — deterministic, collision-resistant for our scale.
    """
    normalised = query.lower().strip()
    return hashlib.sha256(normalised.encode()).hexdigest()[:16]


# ---------- public API ----------

def cache_get(key: str, emit: Callable | None = None) -> dict | None:
    """Read from cache: Redis first, then Postgres, then miss.

    Args:
        key: 16-char hex key (use make_cache_key to produce it)
        emit: optional event emitter (same signature as agent emit)

    Returns:
        Parsed dict if found, None on miss.
    """
    # 1. Try Redis
    client = _get_redis()
    if client:
        try:
            raw = client.get(key)
            if raw:
                return json.loads(raw)
        except (redis.exceptions.RedisError, json.JSONDecodeError) as exc:
            _emit_event(emit, f"cache · redis miss ({type(exc).__name__}): {key}")

    # 2. Fallback: try Upstash REST if configured
    if not os.getenv("REDIS_URL") and os.getenv("UPSTASH_URL"):
        result = _upstash_get(key, emit)
        if result is not None:
            return result

    # 3. Try Postgres (write-through persistence)
    try:
        from app.db import repository
        return repository.get_search_cache(key)
    except Exception as exc:
        _emit_event(emit, f"cache · postgres miss ({type(exc).__name__}): {key}")

    return None


def cache_set(key: str, value: dict, ttl: int = 86400, emit: Callable | None = None) -> None:
    """Write to both Redis and Postgres (write-through).

    Write-through ensures cache survives a Redis flush (TC-P02).
    Failures are swallowed — cache must never break a run.

    Args:
        key: 16-char hex key
        value: serialisable dict to store
        ttl: Redis TTL in seconds (default 24h)
        emit: optional event emitter
    """
    serialised = json.dumps(value)

    # 1. Write to Redis
    client = _get_redis()
    if client:
        try:
            client.setex(key, ttl, serialised)
        except redis.exceptions.RedisError as exc:
            _emit_event(emit, f"cache · redis write fail ({type(exc).__name__}): {key}")

    # 2. Write Upstash if no Redis
    if not os.getenv("REDIS_URL") and os.getenv("UPSTASH_URL"):
        _upstash_set(key, value, ttl, emit)

    # 3. Always write Postgres (write-through)
    try:
        from app.db import repository
        repository.save_search_cache(key, value)
    except Exception as exc:
        _emit_event(emit, f"cache · postgres write fail ({type(exc).__name__}): {key}")


# ---------- Upstash REST fallback ----------

def _upstash_get(key: str, emit: Callable | None) -> dict | None:
    """GET key from Upstash REST API. Returns None on any failure."""
    try:
        import httpx
        url = os.getenv("UPSTASH_URL")
        token = os.getenv("UPSTASH_TOKEN", "")
        resp = httpx.get(
            f"{url}/get/{key}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=3,
        )
        body = resp.json()
        if body.get("result"):
            return json.loads(body["result"])
    except Exception as exc:
        _emit_event(emit, f"cache · upstash get fail ({type(exc).__name__}): {key}")
    return None


def _upstash_set(key: str, value: dict, ttl: int, emit: Callable | None) -> None:
    """SETEX key in Upstash REST API. Swallows failures."""
    try:
        import httpx
        url = os.getenv("UPSTASH_URL")
        token = os.getenv("UPSTASH_TOKEN", "")
        httpx.post(
            f"{url}/setex/{key}/{ttl}",
            content=json.dumps(value),
            headers={"Authorization": f"Bearer {token}"},
            timeout=3,
        )
    except Exception as exc:
        _emit_event(emit, f"cache · upstash set fail ({type(exc).__name__}): {key}")


# ---------- helper ----------

def _emit_event(emit: Callable | None, msg: str) -> None:
    if emit:
        emit("cache", msg)
    logger.debug(msg)