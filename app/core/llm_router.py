"""4-lane LLM router. ONE code path, four configs — every lane speaks the
OpenAI chat-completions dialect (Gemini via its /openai compatibility base).

Behavior (per the v2 plan §4.6):
  task_class -> ordered lane list -> attempt -> on 429/timeout/parse-failure,
  reactive backoff honoring Retry-After, then FAIL OVER to the next lane.
  No blind sleeps. Structured output is validated with Pydantic; a parse
  failure triggers one JSON-repair pass, then lane failover.

MOCK_MODE=1 short-circuits to a deterministic offline lane so the whole
slice runs with zero keys (Saturday 10:00 wiring check).

Base ported from the POC vertical slice. Mihir hardens this in place
(budgets, lane_stats accounting, demo-reserve switch) — same signature.
"""
import json
import os
import random
import re
import time

import httpx
from pydantic import BaseModel, ValidationError

MOCK = os.getenv("MOCK_MODE", "0") == "1"

# Lane order per task class. Cheap/fast lanes first for extraction,
# strong lanes first for reasoning. Lanes without a key are skipped.
LANES = {
    "extract": [
        ("groq",       "https://api.groq.com/openai/v1",        "GROQ_API_KEY",       "llama-3.1-8b-instant"),
        ("gemini",     "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-2.5-flash"),
        ("cerebras",   "https://api.cerebras.ai/v1",            "CEREBRAS_API_KEY",   "llama-3.3-70b"),
        ("openrouter", "https://openrouter.ai/api/v1",          "OPENROUTER_API_KEY", "meta-llama/llama-3.3-70b-instruct:free"),
    ],
    "reason": [
        ("gemini",     "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-2.5-flash"),
        ("cerebras",   "https://api.cerebras.ai/v1",            "CEREBRAS_API_KEY",   "llama-3.3-70b"),
        ("groq",       "https://api.groq.com/openai/v1",        "GROQ_API_KEY",       "llama-3.3-70b-versatile"),
        ("openrouter", "https://openrouter.ai/api/v1",          "OPENROUTER_API_KEY", "deepseek/deepseek-chat-v3.1:free"),
    ],
}
ATTEMPTS_PER_LANE = 2
TIMEOUT = 45.0


def _repair_json(text: str) -> str:
    """Defensive parse sequence from the lessons doc: strip fences, then
    take the outermost JSON object."""
    text = re.sub(r"```(?:json)?", "", text).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return m.group(0) if m else text


def _mock_completion(prompt: str) -> str:
    """Deterministic offline lane. Sniffs the task from the prompt.

    NOTE (Tushar): added the "pricing_tiers" branch below. Previously this
    only handled discovery-shaped prompts ("competitors" in prompt) and fell
    through to `{"answer": "mock"}` for everything else — including
    product.py's extraction prompts, which need pricing_tiers/
    recent_features/positioning/advantages/sources to satisfy ProductIntel.
    That fallback doesn't validate against ProductIntel, so MOCK_MODE runs
    of the product agent crashed with a pydantic ValidationError. Flagging
    since this file is owned by Mihir — happy to hand off if you want a
    different mock shape.
    """
    if "competitors" in prompt.lower():
        company = "the company"
        m = re.search(r"competitors of (.+?) in", prompt)
        if m:
            company = m.group(1).strip()
        seed = {"Notion": ["Coda", "ClickUp", "Slite", "Obsidian"],
                "Zomato": ["Swiggy", "Zepto", "EatSure", "magicpin"]}
        names = seed.get(company, ["Rival One", "Rival Two", "Rival Three", "Rival Four"])
        return json.dumps({"competitors": [
            {"name": n, "category": "direct" if i < 3 else "indirect",
             "rationale": f"Overlapping buyer and jobs-to-be-done with {company} (mock)"}
            for i, n in enumerate(names)]})
    if "pricing_tiers" in prompt:
        # "competitor" is required by ProductIntel even though product.py
        # re-stamps it with the caller's known-good name after validation —
        # validation itself happens before that stamp, so this placeholder
        # just needs to satisfy the schema, not be accurate.
        return json.dumps({
            "competitor": "mock",
            "pricing_tiers": ["Pro $12/seat: AI included (mock)"],
            "recent_features": ["AI formulas v2 (mock)"],
            "positioning": "docs-as-apps for power teams (mock)",
            "advantages": ["cheaper at small team size (mock)"],
            "sources": ["https://example.com/mock-source"],
        })
    return json.dumps({"answer": "mock"})


def complete(task_class: str, prompt: str, schema: type[BaseModel],
             emit=lambda agent, msg: None):
    """Returns (validated_model, lane_name). Raises RuntimeError only if
    every configured lane is exhausted — callers convert that into a
    typed low_signal result, never a raw error to the user."""
    if MOCK:
        emit("router", "MOCK_MODE lane · deterministic completion")
        # NOTE (Tushar): the real-API path below already wraps schema
        # validation in try/except and converts a mismatch into lane
        # failover / RuntimeError. This mock path used to call
        # model_validate_json() unguarded, so any future schema this mock
        # doesn't know how to fake would raise a raw ValidationError
        # instead of the RuntimeError callers (e.g. product.py) expect.
        try:
            return schema.model_validate_json(_mock_completion(prompt)), "mock"
        except ValidationError as ve:
            raise RuntimeError(f"mock lane schema-fail: {ve.errors()[0]['msg']}")

    lanes = [lane for lane in LANES[task_class] if os.getenv(lane[2])]
    if not lanes:
        raise RuntimeError("no LLM lane configured — set at least one *_API_KEY")

    last_err = "unknown"
    for name, base, key_env, model in lanes:
        for attempt in range(1, ATTEMPTS_PER_LANE + 1):
            try:
                emit("router", f"{name}/{model} · attempt {attempt}")
                r = httpx.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {os.environ[key_env]}"},
                    json={"model": model, "temperature": 0.1,
                          "response_format": {"type": "json_object"},
                          "messages": [
                              {"role": "system",
                               "content": "Reply with a single JSON object only. No prose, no code fences."},
                              {"role": "user", "content": prompt}]},
                    timeout=TIMEOUT)
                if r.status_code == 429:
                    wait = float(r.headers.get("retry-after", 0) or 0)
                    wait = min(wait or (1.5 ** attempt + random.random()), 8.0)
                    emit("router", f"{name} 429 · backoff {wait:.1f}s")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"]
                try:
                    return schema.model_validate_json(_repair_json(raw)), name
                except ValidationError as ve:
                    last_err = f"{name} schema-fail: {ve.errors()[0]['msg']}"
                    emit("router", f"{name} parse failed · failing over")
                    break  # parse failure -> next lane, don't retry same lane
            except httpx.HTTPError as e:
                last_err = f"{name}: {e}"
                emit("router", f"{name} error · {type(e).__name__}")
                time.sleep(min(1.5 ** attempt, 4.0))
        # lane exhausted -> next
    raise RuntimeError(f"all lanes exhausted · last: {last_err}")
