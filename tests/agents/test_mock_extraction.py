"""Regression guard: every agent's MOCK lane must produce a REAL result, not
silently degrade to low_signal.

Why this exists: twice now an agent's extraction has failed in MOCK because the
mock router had no branch for its schema (or the prompt example omitted a
required field). The symptom — `low_signal=True` — reads like "the corpus was
thin", not "the mock lane is broken", so it survived casual smoke-testing. These
tests fail loudly if that regresses. Convention: a new gathering agent adds its
mock-router branch AND a case here in the same PR.

Run:  MOCK_MODE=1 pytest tests/agents/test_mock_extraction.py -q
"""
import os

os.environ.setdefault("MOCK_MODE", "1")

from app.agents import product, review  # noqa: E402


def _emit(agent, msg):  # codebase-standard emit(agent, msg); no-op sink for tests
    pass


def test_mock_product_returns_real_result():
    out = product.run(["Acme"], _emit, "Us")[0]  # list[dict]
    assert out["low_signal"] is False
    assert out["pricing_tiers"], "expected non-empty pricing_tiers from the mock lane"
    assert out["competitor"] == "Acme", "competitor must be re-stamped to the caller's name"


def test_mock_review_returns_real_result():
    out = review.run(["Acme"], _emit, "Us")[0]  # SentimentIntel
    assert out.low_signal is False
    assert out.top_complaints, "expected non-empty top_complaints from the mock lane"
    assert out.competitor == "Acme", "competitor must be re-stamped to the caller's name"
