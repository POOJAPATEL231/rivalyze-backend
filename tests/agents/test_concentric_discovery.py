"""Concentric discovery (flag-gated): grounds the company location, then searches
rivals in expanding radius (city -> region -> country -> global), widening only when
a tier is thin. Flag OFF = today's flat discovery, unchanged."""
import os

os.environ.setdefault("MOCK_MODE", "1")   # MUST precede app imports (MOCK is frozen at import)

from app.agents import discovery  # noqa: E402
from app.core import config  # noqa: E402
from app.models import CompanyProfile, GeoLocation  # noqa: E402


def _noop(a, m):
    pass


def test_flag_off_uses_flat_discovery(monkeypatch):
    monkeypatch.setattr(config, "CONCENTRIC_DISCOVERY", False)
    out = discovery.run("Notion", "docs", "t", _noop)          # MOCK lane
    assert [c.name for c in out.competitors]                    # still finds rivals


def test_flag_on_full_path_finds_rivals(monkeypatch):
    monkeypatch.setattr(config, "CONCENTRIC_DISCOVERY", True)
    out = discovery.run("Notion", "docs", "t", _noop)
    assert [c.name for c in out.competitors]                    # concentric path works too


def test_resolve_profile_grounds_location():
    p = discovery._resolve_profile("Notion", "docs", "July 2026", _noop)  # MOCK -> Bengaluru/India
    assert p.location.city == "Bengaluru" and p.location.country == "India"
    assert p.size == "mid"


def test_concentric_corpus_stops_widening_when_enough(monkeypatch):
    # city=1, region=1, country=5 -> accumulates to 7 by country -> STOP before global.
    # content is substantial so the corpus clears the extraction threshold too.
    def fake_search(q, emit):
        n = 5 if "India" in q else 1
        return [{"title": "t", "content": "x" * 100, "url": f"http://x/{hash(q)}/{i}"} for i in range(n)]
    monkeypatch.setattr(discovery.search_mod, "search", fake_search)
    monkeypatch.setattr(config, "CONCENTRIC_MIN_RESULTS", 4)
    prof = CompanyProfile(name="Acme",
                          location=GeoLocation(city="Ahmedabad", region="Gujarat", country="India"))
    corpus = discovery._concentric_corpus("Acme", "widgets", prof, "July 2026", _noop)
    assert "[city:Ahmedabad]" in corpus and "[region:Gujarat]" in corpus and "[country:India]" in corpus
    assert "[global]" not in corpus                            # enough closer -> never widened to global


def test_concentric_corpus_reaches_global_when_all_thin(monkeypatch):
    monkeypatch.setattr(discovery.search_mod, "search",
                        lambda q, emit: [{"title": "t", "content": "c", "url": f"http://x/{hash(q)}"}])
    monkeypatch.setattr(config, "CONCENTRIC_MIN_RESULTS", 4)
    prof = CompanyProfile(name="Acme",
                          location=GeoLocation(city="Ahmedabad", region="Gujarat", country="India"))
    corpus = discovery._concentric_corpus("Acme", "widgets", prof, "July 2026", _noop)
    assert "[global]" in corpus                                # all tiers thin -> widened all the way


def test_concentric_corpus_no_location_starts_global(monkeypatch):
    seen = []
    monkeypatch.setattr(discovery.search_mod, "search",
                        lambda q, emit: (seen.append(q), [])[1])
    prof = CompanyProfile(name="Acme")                         # blank location
    discovery._concentric_corpus("Acme", "widgets", prof, "July 2026", _noop)
    assert len(seen) == 1                                       # only the global tier, no invented place


def test_resolve_profile_thin_search_stays_blank(monkeypatch):
    monkeypatch.setattr(discovery.search_mod, "search", lambda q, e: [])
    p = discovery._resolve_profile("Acme", "widgets", "July 2026", _noop)
    assert p.location.city == "" and p.location.country == ""   # NEVER invents a location
    assert p.name == "Acme"


def test_resolve_profile_never_raises(monkeypatch):
    monkeypatch.setattr(discovery.search_mod, "search",
                        lambda q, e: [{"title": "t", "content": "x" * 400, "url": "http://x"}])

    def boom(*a, **k):
        raise RuntimeError("all lanes exhausted")
    monkeypatch.setattr(discovery.llm_router, "complete", boom)
    p = discovery._resolve_profile("Acme", "widgets", "July 2026", _noop)
    assert p.name == "Acme" and p.location.country == ""        # degrades blank, no crash


def test_concentric_dedups_urls_across_tiers(monkeypatch):
    # every tier returns the SAME url -> counted once, so it keeps widening to global
    monkeypatch.setattr(discovery.search_mod, "search",
                        lambda q, e: [{"title": "t", "content": "c", "url": "http://same"}])
    monkeypatch.setattr(config, "CONCENTRIC_MIN_RESULTS", 4)
    prof = CompanyProfile(name="Acme", location=GeoLocation(city="A", region="B", country="C"))
    corpus = discovery._concentric_corpus("Acme", "w", prof, "July 2026", _noop)
    assert corpus.count("SOURCE: http://same") == 1            # same url across tiers counted once
    assert "[city:A]" in corpus                                 # kept at the CLOSEST tier it appeared


def test_concentric_region_equal_city_is_skipped(monkeypatch):
    seen = []
    monkeypatch.setattr(discovery.search_mod, "search", lambda q, e: (seen.append(q), [])[1])
    prof = CompanyProfile(name="Acme", location=GeoLocation(city="Pune", region="pune", country="India"))
    discovery._concentric_corpus("Acme", "w", prof, "July 2026", _noop)
    assert len(seen) == 3                                       # city + country + global (region == city, skipped)


def test_min_corpus_guard_applies_in_concentric(monkeypatch):
    # flag on, but search finds nothing anywhere -> degrade to EMPTY, never fabricate rivals
    monkeypatch.setattr(config, "CONCENTRIC_DISCOVERY", True)
    monkeypatch.setattr(discovery.search_mod, "search", lambda q, e: [])
    out = discovery.run("Acme", "widgets", "t", _noop)
    assert out.competitors == []


def test_resolve_profile_clamps_long_model_fields(monkeypatch):
    # a rambling size string (>60) must NOT throw away the grounded location
    monkeypatch.setattr(discovery.search_mod, "search",
                        lambda q, e: [{"title": "t", "content": "x" * 400, "url": "http://x"}])

    class _P:
        city, region, country = "Bengaluru", "Karnataka", "India"
        size = "approximately 1,000-1,500 employees across offices in Bengaluru, Mumbai and Delhi"
    monkeypatch.setattr(discovery.llm_router, "complete", lambda *a, **k: (_P(), "mock"))
    p = discovery._resolve_profile("Acme", "widgets", "July 2026", _noop)
    assert p.location.city == "Bengaluru"          # location KEPT (not discarded by the long size)
    assert len(p.size) <= 60                        # clamped, still valid


def test_concentric_widens_when_count_met_but_text_thin(monkeypatch):
    # city tier hits the result COUNT (5 >= 4) but its text is empty -> must NOT stop; keep widening
    seen = []

    def fake(q, e):
        seen.append(q)
        return [{"title": "", "content": "", "url": f"http://x/{len(seen)}/{i}"} for i in range(5)]
    monkeypatch.setattr(discovery.search_mod, "search", fake)
    monkeypatch.setattr(config, "CONCENTRIC_MIN_RESULTS", 4)
    prof = CompanyProfile(name="Acme", location=GeoLocation(city="A", region="B", country="C"))
    discovery._concentric_corpus("Acme", "w", prof, "July 2026", _noop)
    assert any(" in B " in f" {q} " for q in seen)  # region reached despite city hitting the count
