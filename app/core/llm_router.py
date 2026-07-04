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
    take the outermost JSON object. Uses find/rfind slicing (linear) instead of
    a greedy DOTALL regex, and caps length, to avoid pathological backtracking
    on a large/unbalanced model response."""
    text = re.sub(r"```(?:json)?", "", text).strip()[:100_000]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


def _mock_completion(prompt: str) -> str:
    """Deterministic offline lane. Sniffs the task from the prompt.

    Each agent sends a differently-shaped extraction prompt, and this
    function has to guess which one it's looking at using nothing but a
    keyword unique to that prompt's own instructions (there is no explicit
    "task type" argument passed down from complete()). Whenever a new agent
    is added, it needs its own branch here with a JSON body that satisfies
    that agent's Pydantic schema — otherwise MOCK_MODE falls through to the
    generic `{"answer": "mock"}` below, which fails schema validation for
    every schema except a trivial one, and the caller silently degrades to
    low_signal instead of exercising the real code path.

    NOTE (Tushar): added the "pricing_tiers" branch below. Previously this
    only handled discovery-shaped prompts ("competitors" in prompt) and fell
    through to `{"answer": "mock"}` for everything else — including
    product.py's extraction prompts, which need pricing_tiers/
    recent_features/positioning/advantages/sources to satisfy ProductIntel.
    That fallback doesn't validate against ProductIntel, so MOCK_MODE runs
    of the product agent crashed with a pydantic ValidationError. Flagging
    since this file is owned by Mihir — happy to hand off if you want a
    different mock shape.

    NOTE (Tushar): same bug, second occurrence — added the "top_complaints"
    branch below for review.py. review.py's _SYSTEM_PROMPT always contains
    the literal word "top_complaints" (it's part of the field-name examples
    baked into the prompt text), which is what this branch keys off of. The
    returned "competitor" value is a throwaway placeholder — review.py's
    _sanitise() re-stamps it with the real competitor name afterwards, the
    same as the product-agent mock branch does.

    Before this branch existed, every review.run() call under MOCK_MODE hit
    the generic `{"answer": "mock"}` fallback, which is missing SentimentIntel's
    required "competitor" field — complete() raised RuntimeError, and
    review._run_single()'s except-block silently converted that into
    SentimentIntel(competitor=..., low_signal=True) for every competitor.
    The bug was invisible from the outside: it never raised past review.run()
    and low_signal=True looks like "the corpus was thin," not "the mock lane
    is broken," so it could pass casual smoke-testing indefinitely. Confirmed
    fixed by re-running the discovery -> product -> review chain in MOCK_MODE
    and checking review's output has low_signal=False with real-looking
    complaints/opportunity_gaps instead of empty lists.
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
    if "top_complaints" in prompt:
        # "competitor" is required by SentimentIntel even though review.py's
        # _sanitise() re-stamps it with the caller's known-good name after
        # validation (same pattern as the product-agent branch above) —
        # validation happens before that stamp, so this placeholder only
        # needs to satisfy the schema, not be accurate.
        return json.dumps({
            "competitor": "mock",
            "top_complaints": ["feature overload (mock)"],
            "opportunity_gaps": ["ship a lightweight tier (mock)"],
            "overall_sentiment": "NEUTRAL",
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
                payload = {"model": model, "temperature": 0.1,
                           "max_tokens": 1024,  # bound response size/latency/cost
                           "response_format": {"type": "json_object"},
                           "messages": [
                               {"role": "system",
                                "content": "Reply with a single JSON object only. No prose, no code fences."},
                               {"role": "user", "content": prompt}]}
                if name == "gemini":
                    # Gemini 2.5 models "think" by default even over the OpenAI-
                    # compat endpoint: invisible reasoning tokens are drawn from
                    # the SAME max_tokens budget as the visible JSON, and the API
                    # doesn't report them under completion_tokens, so a truncated
                    # response looks like plenty of budget was left. Confirmed by
                    # hand: with reasoning left on, a real discovery-shaped prompt
                    # came back finish_reason="length" after burning ~980 of 1024
                    # tokens on thinking, with the visible JSON cut off mid-string
                    # (schema-fail: "EOF while parsing a string") — every single
                    # call failed over for this reason, not a genuine outage.
                    # reasoning_effort="none" turns thinking off so the full
                    # budget goes to the JSON we actually asked for.
                    payload["reasoning_effort"] = "none"
                r = httpx.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {os.environ[key_env]}"},
                    json=payload,
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
