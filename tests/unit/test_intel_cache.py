"""Per-competitor intel cache: only newly-added rivals hit the agents; cached
rivals are reused; order is preserved; and with the flag off it's a pass-through."""
import pytest

from app.core import cache, config, intel_cache


@pytest.fixture
def fake_cache(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(cache, "cache_get", lambda k, emit=None: store.get(k))
    monkeypatch.setattr(cache, "cache_set",
                        lambda k, v, ttl=86400, emit=None: store.__setitem__(k, v))
    return store


def _noop(*a):
    pass


def test_passthrough_when_flag_off(monkeypatch, fake_cache):
    monkeypatch.setattr(config, "COMPETITOR_INTEL_CACHE", False)
    calls = []

    def run_subset(names):
        calls.append(list(names))
        return [{"competitor": n} for n in names]

    out = intel_cache.gather("product", ["A", "B"], run_subset, _noop)
    assert calls == [["A", "B"]]                       # all gathered, no caching
    assert [x["competitor"] for x in out] == ["A", "B"]
    assert fake_cache == {}                             # nothing written when off


def test_cold_then_warm(monkeypatch, fake_cache):
    monkeypatch.setattr(config, "COMPETITOR_INTEL_CACHE", True)
    calls = []

    def run_subset(names):
        calls.append(list(names))
        return [{"competitor": n} for n in names]

    # cold: gather both, cache them
    intel_cache.gather("product", ["A", "B"], run_subset, _noop)
    assert calls == [["A", "B"]]
    # warm: both cached -> run_subset never called
    calls.clear()
    out = intel_cache.gather("product", ["A", "B"], run_subset, _noop)
    assert calls == []                                 # zero agent calls
    assert [x["competitor"] for x in out] == ["A", "B"]


def test_only_new_rivals_are_gathered(monkeypatch, fake_cache):
    monkeypatch.setattr(config, "COMPETITOR_INTEL_CACHE", True)
    # pre-seed A as cached
    fake_cache[intel_cache._key("product", "A")] = {"competitor": "A", "cached": True}
    calls = []

    def run_subset(names):
        calls.append(list(names))
        return [{"competitor": n, "fresh": True} for n in names]

    out = intel_cache.gather("product", ["A", "B"], run_subset, _noop)
    assert calls == [["B"]]                             # ONLY the new rival
    assert [x["competitor"] for x in out] == ["A", "B"]  # order preserved
    assert out[0].get("cached") and out[1].get("fresh")


def test_cache_read_error_falls_back_to_gathering(monkeypatch, fake_cache):
    monkeypatch.setattr(config, "COMPETITOR_INTEL_CACHE", True)

    def boom(k, emit=None):
        raise RuntimeError("redis down")

    monkeypatch.setattr(cache, "cache_get", boom)
    calls = []

    def run_subset(names):
        calls.append(list(names))
        return [{"competitor": n} for n in names]

    out = intel_cache.gather("news", ["A"], run_subset, _noop)
    assert calls == [["A"]]                             # error -> just gather
    assert [x["competitor"] for x in out] == ["A"]
