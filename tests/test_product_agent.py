import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def emit():
    return MagicMock()


@pytest.fixture(autouse=True)
def fat_search(monkeypatch):
    """Default: search returns enough content to clear the 300-char threshold."""
    monkeypatch.setattr("app.agents.product.search", lambda q, e: [
        {"title": "Test", "url": "https://coda.io/pricing", "content": "Pro $12/seat AI included " * 25}
    ])


def _good_intel(name="Coda"):
    from app.models import ProductIntel
    return ProductIntel(competitor=name, pricing_tiers=["Pro $12/seat: AI included"],
                        recent_features=["AI formulas"], positioning="docs-as-apps",
                        advantages=["simpler onboarding"], sources=["https://coda.io/pricing"])


def test_returns_list_of_dicts(emit):
    from app.agents.product import run
    with patch("app.agents.product.complete", return_value=(_good_intel(), "mock")):
        result = run(["Coda"], emit)
    assert isinstance(result, list) and len(result) == 1
    assert result[0]["competitor"] == "Coda"


def test_pricing_tiers_are_plain_strings(emit):
    """#1 correctness check — must be list[str], never list[dict]."""
    from app.agents.product import run
    with patch("app.agents.product.complete", return_value=(_good_intel(), "mock")):
        result = run(["Coda"], emit)
    for tier in result[0]["pricing_tiers"]:
        assert isinstance(tier, str), f"Got nested object: {tier}"


def test_low_signal_on_thin_corpus(emit, monkeypatch):
    """Empty search -> corpus < 300 chars -> low_signal=True, complete() never called."""
    monkeypatch.setattr("app.agents.product.search", lambda q, e: [])
    from app.agents.product import run
    with patch("app.agents.product.complete") as mc:
        result = run(["Ghost Corp"], emit)
        mc.assert_not_called()
    assert result[0]["low_signal"] is True


def test_low_signal_on_llm_failure(emit):
    """complete() raises RuntimeError (all lanes exhausted) -> low_signal=True, no propagation."""
    from app.agents.product import run
    with patch("app.agents.product.complete", side_effect=RuntimeError("all lanes exhausted")):
        result = run(["BadCorp"], emit)
    assert result[0]["low_signal"] is True
    assert result[0]["competitor"] == "BadCorp"


def test_competitor_from_dict(emit):
    """Competitors can be dicts with 'name' key (LangGraph orchestrator format)."""
    from app.agents.product import run
    with patch("app.agents.product.complete", return_value=(_good_intel("ClickUp"), "mock")):
        result = run([{"name": "ClickUp", "category": "direct"}], emit)
    assert result[0]["competitor"] == "ClickUp"
