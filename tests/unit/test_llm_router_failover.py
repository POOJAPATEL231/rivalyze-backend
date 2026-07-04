"""llm_router HTTP failover behavior (respx-mocked, no real network).

Locks in the 4xx-vs-5xx handling: a client error (e.g. a rejected
`reasoning_effort` param → 400) fails over to the next lane IMMEDIATELY without
a pointless retry, while a server error (5xx) retries the same lane before
failing over. MOCK is patched off so the real HTTP path runs.
"""
import json

import httpx
import respx
from pydantic import BaseModel

from app.core import llm_router

_GROQ = "https://api.groq.com/openai/v1/chat/completions"
_GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


class _One(BaseModel):
    answer: str


def _ok_body() -> dict:
    return {"choices": [{"message": {"content": json.dumps({"answer": "ok"})}}]}


def _only_groq_and_gemini(monkeypatch):
    monkeypatch.setattr(llm_router, "MOCK", False)
    monkeypatch.setenv("GROQ_API_KEY", "x")      # extract lane 1
    monkeypatch.setenv("GEMINI_API_KEY", "y")    # extract lane 2
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@respx.mock
def test_4xx_fails_over_immediately_without_retry(monkeypatch):
    _only_groq_and_gemini(monkeypatch)
    groq = respx.post(_GROQ).mock(return_value=httpx.Response(400, json={"error": "bad param"}))
    respx.post(_GEMINI).mock(return_value=httpx.Response(200, json=_ok_body()))

    result, lane = llm_router.complete("extract", "prompt", _One, lambda a, m: None)

    assert lane == "gemini" and result.answer == "ok"   # failed over to lane 2
    assert groq.call_count == 1                          # 4xx -> NOT retried


@respx.mock
def test_5xx_retries_same_lane_then_fails_over(monkeypatch):
    _only_groq_and_gemini(monkeypatch)
    groq = respx.post(_GROQ).mock(return_value=httpx.Response(503))
    respx.post(_GEMINI).mock(return_value=httpx.Response(200, json=_ok_body()))

    result, lane = llm_router.complete("extract", "prompt", _One, lambda a, m: None)

    assert lane == "gemini" and result.answer == "ok"
    assert groq.call_count == llm_router.ATTEMPTS_PER_LANE   # 5xx -> retried, then failover
