"""Concentric discovery (flag-gated): grounds the company location, then searches
rivals in expanding radius (city -> region -> country -> global), widening only when
a tier is thin. Flag OFF = today's flat discovery, unchanged."""
from app.agents import discovery
from app.core import config
from app.models import CompanyProfile, GeoLocation


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
    # city=1, region=1, country=5 -> accumulates to 7 by country -> STOP before global
    def fake_search(q, emit):
        n = 5 if "India" in q else 1
        return [{"title": "t", "content": "c", "url": f"http://x/{hash(q)}/{i}"} for i in range(n)]
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
