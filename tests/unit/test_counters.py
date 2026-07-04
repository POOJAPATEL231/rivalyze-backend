"""Tests for app/core/counters.py's negative-caching fix (tech-lead review on
PR #19): a set-but-unreachable REDIS_URL must not force a fresh ~3s
reconnect+ping attempt on every single counter_get/counter_incr call — that
turned a 7-provider /credits read into a ~21s stall right when Redis is
already degraded.
"""
import time

import pytest

from app.core import counters


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.setattr(counters, "_redis_client", None)
    monkeypatch.setattr(counters, "_last_connect_failure_at", None)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:1/0")


class _FailingClient:
    def ping(self):
        raise ConnectionError("nope")


class _OkClient:
    def ping(self):
        return True


def test_repeated_failures_within_cooldown_do_not_reconnect(monkeypatch):
    calls = []
    monkeypatch.setattr("redis.from_url", lambda *a, **kw: calls.append(1) or _FailingClient())

    assert counters._get_client() is None
    assert counters._get_client() is None

    assert len(calls) == 1  # second call hit the negative cache, no reconnect attempt


def test_reconnects_after_cooldown_elapses(monkeypatch):
    calls = []
    monkeypatch.setattr("redis.from_url", lambda *a, **kw: calls.append(1) or _FailingClient())
    monkeypatch.setattr(counters, "_RECONNECT_COOLDOWN_S", 0)

    assert counters._get_client() is None
    assert counters._get_client() is None

    assert len(calls) == 2  # cooldown is 0, so every call retries


def test_successful_connection_clears_negative_cache(monkeypatch):
    monkeypatch.setattr("redis.from_url", lambda *a, **kw: _OkClient())
    # A failure timestamp older than the cooldown -> the retry is actually
    # attempted (not short-circuited by the negative cache itself).
    monkeypatch.setattr(counters, "_last_connect_failure_at", time.monotonic() - counters._RECONNECT_COOLDOWN_S - 1)

    client = counters._get_client()

    assert client is not None
    assert counters._last_connect_failure_at is None


def test_counter_get_still_returns_zero_during_cooldown(monkeypatch):
    monkeypatch.setattr("redis.from_url", lambda *a, **kw: _FailingClient())

    assert counters.counter_get(counters.today_key("tavily")) == 0
    assert counters.counter_get(counters.today_key("tavily")) == 0  # still 0, no raise
