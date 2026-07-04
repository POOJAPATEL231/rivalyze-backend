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
    if "RIVALYZE_STRATEGIST" in prompt:
        # Strategist (reason lane). Must be sniffed BEFORE the discovery branch —
        # this prompt also contains the word "competitors". Parse the rival names
        # and the real EVIDENCE_IDS out of the prompt so the mock cites ids that
        # actually exist (strategist.py drops recs citing unknown ids); the report
        # then survives the validate node and confidence is code-recomputed.
        m = re.search(r"COMPETITORS:\s*(.+)", prompt)
        rivals = [r.strip() for r in m.group(1).split(",")] if m else []
        rivals = [r for r in rivals if r and "none" not in r.lower()]
        m = re.search(r"EVIDENCE_IDS:\s*(.+)", prompt)
        ids = [i.strip() for i in m.group(1).split(",")] if m else []
        cite = [i for i in ids if i.startswith("ev-")][:1]
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
        return json.dumps({
            "competitor": "mock",
            "pricing_tiers": ["Pro $12/seat: AI included (mock)"],
            "recent_features": ["AI formulas v2 (mock)"],
            "positioning": "docs-as-apps for power teams (mock)",
            "advantages": ["cheaper at small team size (mock)"],
            "sources": ["https://example.com/mock-source"],
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
                          "max_tokens": 1024,  # bound response size/latency/cost
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
