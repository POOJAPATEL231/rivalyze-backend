"""Multi-key rotation in llm_router (respx-mocked, no real network).

Proves that when a provider has more than one API key, a 429 (out of quota /
rate-limited) on one key rotates to the NEXT key for the SAME provider before
failing over to a different provider. Single-key behaviour is unchanged.
"""
import json

import httpx
import respx
from pydantic import BaseModel

from app.core import llm_router

_GROQ = "https://api.groq.com/openai/v1/chat/completions"


class _One(BaseModel):
    answer: str


def _ok_body() -> dict:
    return {"choices": [{"message": {"content": json.dumps({"answer": "ok"})}}]}


def _only_groq(monkeypatch):
    monkeypatch.setattr(llm_router, "MOCK", False)
    for k in ("GEMINI_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_keys_for_parses_comma_list(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "a, b ,a")   # comma list, with dup + spaces
    assert llm_router._keys_for("GROQ_API_KEY") == ["a", "b"]  # dedup, ordered


@respx.mock
def test_429_rotates_to_next_key_same_provider(monkeypatch):
    _only_groq(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "key-one,key-two")  # two keys, one provider

    calls: dict[str, int] = {}

    def responder(request):
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        calls[key] = calls.get(key, 0) + 1
        if key == "key-one":
            return httpx.Response(429, json={"error": "insufficient_quota"})
        return httpx.Response(200, json=_ok_body())

    respx.post(_GROQ).mock(side_effect=responder)

    result, lane = llm_router.complete("extract", "p", _One, lambda a, m: None)

    assert lane == "groq" and result.answer == "ok"
    assert calls.get("key-one", 0) >= 1     # first key tried
    assert calls.get("key-two", 0) == 1     # rotated to the second key, succeeded


@respx.mock
def test_single_key_unchanged(monkeypatch):
    _only_groq(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "solo")
    route = respx.post(_GROQ).mock(return_value=httpx.Response(200, json=_ok_body()))

    result, lane = llm_router.complete("extract", "p", _One, lambda a, m: None)

    assert lane == "groq" and result.answer == "ok"
    assert route.call_count == 1
