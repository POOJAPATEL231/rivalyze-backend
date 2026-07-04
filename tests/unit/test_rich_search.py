"""RICH_SEARCH flag: off by default (3 results / basic / 6.5k corpus), on = richer
(6 / advanced / 12k), and each value independently overridable."""
import importlib


def _reload_config():
    from app.core import config
    return importlib.reload(config)


def test_defaults_are_conservative(monkeypatch):
    for k in ("RICH_SEARCH", "SEARCH_MAX_RESULTS", "SEARCH_DEPTH", "CORPUS_CAP"):
        monkeypatch.delenv(k, raising=False)
    cfg = _reload_config()
    try:
        assert cfg.RICH_SEARCH is False
        assert cfg.SEARCH_MAX_RESULTS == 3
        assert cfg.SEARCH_DEPTH == "basic"
        assert cfg.CORPUS_CAP == 6500
    finally:
        _reload_config()


def test_rich_search_raises_all_limits(monkeypatch):
    monkeypatch.setenv("RICH_SEARCH", "1")
    cfg = _reload_config()
    try:
        assert cfg.RICH_SEARCH is True
        assert cfg.SEARCH_MAX_RESULTS == 6
        assert cfg.SEARCH_DEPTH == "advanced"
        assert cfg.CORPUS_CAP == 12000
    finally:
        monkeypatch.delenv("RICH_SEARCH", raising=False)
        _reload_config()


def test_individual_override_wins(monkeypatch):
    monkeypatch.setenv("SEARCH_MAX_RESULTS", "10")
    monkeypatch.setenv("CORPUS_CAP", "20000")
    cfg = _reload_config()
    try:
        assert cfg.SEARCH_MAX_RESULTS == 10       # explicit value beats the flag default
        assert cfg.CORPUS_CAP == 20000
    finally:
        monkeypatch.delenv("SEARCH_MAX_RESULTS", raising=False)
        monkeypatch.delenv("CORPUS_CAP", raising=False)
        _reload_config()
