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
# Lane order per task class — the router fails over lane-to-lane on
# 429/timeout/parse/4xx, so listing a provider twice with different models gives
# model-to-model fallback for free (if the first model errors, the next is tried).
# NOTE: Cerebras model ids are account/plan-specific — `llama-3.3-70b` 404s on
# plans that don't include it. `gpt-oss-120b` / `zai-glm-4.7` are broadly
# available and both do clean JSON extraction. Override any model without a code
# change via env: e.g. CEREBRAS_MODEL, GROQ_EXTRACT_MODEL (see _model()).
def _model(env_name: str, default: str) -> str:
    return os.getenv(env_name, default)


LANES = {
    "extract": [
        ("groq",       "https://api.groq.com/openai/v1",        "GROQ_API_KEY",       _model("GROQ_EXTRACT_MODEL", "llama-3.1-8b-instant")),
        ("gemini",     "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", _model("GEMINI_MODEL", "gemini-2.5-flash")),
        ("cerebras",   "https://api.cerebras.ai/v1",            "CEREBRAS_API_KEY",   _model("CEREBRAS_MODEL", "gpt-oss-120b")),
        ("cerebras",   "https://api.cerebras.ai/v1",            "CEREBRAS_API_KEY",   _model("CEREBRAS_MODEL_ALT", "zai-glm-4.7")),
        ("openrouter", "https://openrouter.ai/api/v1",          "OPENROUTER_API_KEY", _model("OPENROUTER_EXTRACT_MODEL", "meta-llama/llama-3.3-70b-instruct:free")),
    ],
    "reason": [
        ("gemini",     "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", _model("GEMINI_MODEL", "gemini-2.5-flash")),
        ("cerebras",   "https://api.cerebras.ai/v1",            "CEREBRAS_API_KEY",   _model("CEREBRAS_MODEL", "gpt-oss-120b")),
        ("groq",       "https://api.groq.com/openai/v1",        "GROQ_API_KEY",       _model("GROQ_REASON_MODEL", "llama-3.3-70b-versatile")),
        ("openrouter", "https://openrouter.ai/api/v1",          "OPENROUTER_API_KEY", _model("OPENROUTER_REASON_MODEL", "deepseek/deepseek-chat-v3.1:free")),
    ],
}
ATTEMPTS_PER_LANE = 2
TIMEOUT = 45.0

# Response-size budget per task class. Extraction outputs are small (a handful of
# typed items); the strategist's CompetitiveReport is large — full SWOT, sentiment
# per rival, 4-6 verbose head-to-head rows, opportunities AND recommendations. At
# the old flat 1024 the report was TRUNCATED mid-JSON: _repair_json salvaged only
# up to the last complete "}", silently dropping opportunities/recommendations (the
# final fields) — the #1 cause of empty recommendations. Reasoning gets a much
# larger budget; extraction a modest bump for the now-richer agent outputs.
_MAX_TOKENS = {"reason": 4096, "extract": 1536}


def _max_tokens(task_class: str) -> int:
    return _MAX_TOKENS.get(task_class, 1536)


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
    if "RIVALYZE_STRATEGIST" in prompt:
        # Strategist (reason lane). Must be sniffed BEFORE the discovery branch —
        # this prompt also contains the word "competitors". Parse the rival names
        # and a real ev- id out of the prompt so the mock cites an id that actually
        # exists; confidence is then code-recomputed from it.
        m = re.search(r"COMPETITORS:\s*(.+)", prompt)
        rivals = [r.strip() for r in m.group(1).split(",")] if m else []
        rivals = [r for r in rivals if r and "none" not in r.lower()]
        # ev- ids now live in the EVIDENCE LEDGER (the flat EVIDENCE_IDS: line was
        # removed when the ledger replaced it) — pull one straight from the ledger.
        cite = re.findall(r"ev-[0-9a-f]+", prompt)[:1]
        first = rivals[0] if rivals else "the leading rival"
        return json.dumps({
            "company": "mock",
            "threat_level": "MEDIUM",
            "executive_summary": (
                f"Mock analysis: {first} and peers compete on price and feature depth. "
                "The clearest opening is a lighter, lower-priced tier for smaller teams."),
            "swot": {
                "strengths": ["recognized brand (mock)"],
                "weaknesses": ["feature bloat (mock)"],
                "opportunities": ["simpler onboarding (mock)"],
                "threats": ["aggressive rival pricing (mock)"]},
            "sentiment": {r: {"score": 0.5, "label": "NEUTRAL"} for r in rivals},
            "head_to_head": ([{
                "metric": "Pricing",
                "you": "Competitive entry price",
                "rivals": {r: {"value": "$12/seat (mock)"} for r in rivals}}]
                if rivals else []),
            "opportunities": [
                {"text": "Ship a lightweight no-AI tier (mock)",
                 "evidence_ids": cite, "claim_ref": "opp:lite-tier"}],
            "recommendations": [
                {"action": "Bundle AI into the base plan (mock)",
                 "rationale": "Neutralizes the rival pricing edge (mock)",
                 "confidence": 0.5, "evidence_ids": cite, "claim_ref": "rec:bundle-ai"}],
            "low_signal_findings": [],
            "analysis_date": "2026-01-01"})
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
        # Pull a real SOURCE url from the corpus (like the news mock): product.py
        # now grounds sources against the corpus, so an invented url would be
        # stripped and MOCK-mode evidence would come back empty.
        src = re.findall(r"SOURCE:\s*(https?://\S+)", prompt)[:1]
        return json.dumps({
            "competitor": "mock",
            "pricing_tiers": ["Pro $12/seat: AI included (mock)"],
            "recent_features": ["AI formulas v2 (mock)"],
            "positioning": "docs-as-apps for power teams (mock)",
            "advantages": ["cheaper at small team size (mock)"],
            "sources": src,
        })
    if "source_url" in prompt:
        # News extraction (_NewsExtraction: {"items": [NewsItem]}).
        # news.py's _post_filter keeps an item ONLY if its source_url appears
        # verbatim on a SOURCE: line in the corpus, so pull the real URLs out of the
        # prompt's corpus instead of inventing them — an invented URL gets stripped
        # by the grounding filter and the demo shows zero events. No SOURCE lines in
        # the corpus -> items:[] -> the competitor degrades to low_signal, as in
        # real mode.
        urls = re.findall(r"SOURCE:\s*(https?://\S+)", prompt)[:2]
        events = [
            ("Shipped an AI feature update (mock)",
             "Raises the AI bar for competing products (mock)", "2026-07-01"),
            ("Raised a new funding round (mock)",
             "War chest for pricing and go-to-market pressure (mock)", ""),
        ]
        return json.dumps({"items": [
            {"event": event, "impact": impact, "source_url": url, "date": date}
            for url, (event, impact, date) in zip(urls, events)]})
    if "top_complaints" in prompt:
        # "competitor" is required by SentimentIntel even though review.py's
        # _sanitise() re-stamps it with the caller's known-good name after
        # validation (same pattern as the product-agent branch above) —
        # validation happens before that stamp, so this placeholder only
        # needs to satisfy the schema, not be accurate.
        # Pull a real SOURCE url from the corpus (review.py now grounds sources).
        src = re.findall(r"SOURCE:\s*(https?://\S+)", prompt)[:1]
        return json.dumps({
            "competitor": "mock",
            "top_complaints": ["feature overload (mock)"],
            "opportunity_gaps": ["ship a lightweight tier (mock)"],
            "overall_sentiment": "NEUTRAL",
            "sources": src,
        })
    return json.dumps({"answer": "mock"})


def _keys_for(key_env: str) -> list[str]:
    """All API keys configured for a provider, in rotation order.

    Supports MULTIPLE keys per provider two ways (mix freely):
      - comma-separated in the base var:  GROQ_API_KEY=key1,key2,key3
      - numbered variants:                GROQ_API_KEY_2=key2, GROQ_API_KEY_3=...
    The router tries the next key for the SAME provider when one is
    rate-limited / out of quota (or rejected) before failing over to a
    different provider. A single key (no comma, no _2) behaves exactly as before.
    """
    keys: list[str] = []
    seen: set[str] = set()
    candidates = [os.getenv(key_env, "")]
    i = 2
    while os.getenv(f"{key_env}_{i}"):
        candidates.append(os.getenv(f"{key_env}_{i}", ""))
        i += 1
    for c in candidates:
        for k in c.split(","):
            k = k.strip()
            if k and k not in seen:
                seen.add(k)
                keys.append(k)
    return keys


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

    lanes = [lane for lane in LANES[task_class] if _keys_for(lane[2])]
    if not lanes:
        raise RuntimeError("no LLM lane configured — set at least one *_API_KEY")

    last_err = "unknown"
    for name, base, key_env, model in lanes:
        keys = _keys_for(key_env)
        for ki, key in enumerate(keys, 1):
            tag = f" · key {ki}/{len(keys)}" if len(keys) > 1 else ""
            next_provider = False
            for attempt in range(1, ATTEMPTS_PER_LANE + 1):
                try:
                    emit("router", f"{name}/{model} · attempt {attempt}{tag}")
                    payload = {"model": model, "temperature": 0.1,
                               "max_tokens": _max_tokens(task_class),  # task-aware: reason needs room for the full report
                               "response_format": {"type": "json_object"},
                               "messages": [
                                   {"role": "system",
                                    "content": "Reply with a single JSON object only. No prose, no code fences."},
                                   {"role": "user", "content": prompt}]}
                    if name == "gemini":
                        # Gemini 2.5 "thinks" by default over the OpenAI-compat
                        # endpoint, drawing invisible reasoning tokens from the same
                        # max_tokens budget and truncating the JSON. Turn it off so
                        # the full budget goes to the JSON we asked for.
                        payload["reasoning_effort"] = "none"
                    r = httpx.post(
                        f"{base}/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json=payload,
                        timeout=TIMEOUT)
                    # 429 = out of quota / rate-limited, 401/403 = bad or blocked
                    # key — all key-specific. If another key exists for THIS
                    # provider, rotate to it immediately before failing over to a
                    # different (weaker/slower) provider.
                    if r.status_code in (429, 401, 403):
                        last_err = f"{name}: HTTP {r.status_code} (key {ki}/{len(keys)})"
                        if ki < len(keys):
                            emit("router", f"{name} HTTP {r.status_code} · key {ki}/{len(keys)} exhausted · rotating key")
                            break  # -> next key, same provider
                        if r.status_code == 429:
                            wait = float(r.headers.get("retry-after", 0) or 0)
                            wait = min(wait or (1.5 ** attempt + random.random()), 8.0)
                            emit("router", f"{name} 429 · last key · backoff {wait:.1f}s")
                            time.sleep(wait)
                            continue  # retry the last key after backoff
                        emit("router", f"{name} HTTP {r.status_code} · no keys left · failing over")
                        next_provider = True
                        break
                    r.raise_for_status()
                    # Extract the text defensively: a 200 with an unexpected body
                    # shape (missing choices/message, content=None on a refusal,
                    # or non-JSON) must fail over — NOT raise an uncaught
                    # KeyError/IndexError out of complete() that skips failover and
                    # zeroes the caller's result (this crashed strategist/reviews).
                    try:
                        raw = r.json()["choices"][0]["message"]["content"]
                    except (KeyError, IndexError, TypeError, ValueError):
                        raw = None
                    if not raw:
                        last_err = f"{name}: empty/malformed response body"
                        emit("router", f"{name} malformed response · failing over")
                        next_provider = True
                        break
                    try:
                        return schema.model_validate_json(_repair_json(raw)), name
                    except ValidationError as ve:
                        last_err = f"{name} schema-fail: {ve.errors()[0]['msg']}"
                        emit("router", f"{name} parse failed · failing over")
                        next_provider = True  # a new key won't change the output
                        break
                except httpx.HTTPError as e:
                    last_err = f"{name}: {e}"
                    emit("router", f"{name} error · {type(e).__name__}")
                    time.sleep(min(1.5 ** attempt, 4.0))
            if next_provider:
                break  # stop trying this provider's keys -> next provider
        # all keys for this provider exhausted -> next provider
    raise RuntimeError(f"all lanes exhausted · last: {last_err}")
