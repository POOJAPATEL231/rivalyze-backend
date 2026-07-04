"""Monitor Delta v0 — diff two runs' signal sets. Pure logic, no I/O.

A signal in the latest run R1 is NEW iff no signal in the previous run R0 has
the same identity key:

    (agent, competitor, type, normalized_headline)

normalized_headline = lower -> strip everything except [a-z0-9 ] -> trim ->
first 80 chars of the signal's headline text (payload['event'] preferred,
payload['headline'] fallback). The normalization is WHY naive diffing fails:
the same funding news re-found with different punctuation/casing must NOT
count as new. Normalization is matching-only — responses carry the raw text.

Doc: Rivalyze_Monitor_Delta_Backend.md. Owners: Dharvi (query), Drashti (route).
"""
import re

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_HEADLINE_KEY_CHARS = 80


def _raw_headline(payload: dict) -> str:
    return str(payload.get("event") or payload.get("headline") or "")


def normalize_headline(payload: dict) -> str:
    """lower -> strip punctuation (keep [a-z0-9 ]) -> trim -> first 80 chars."""
    return _NON_ALNUM.sub("", _raw_headline(payload).lower()).strip()[:_HEADLINE_KEY_CHARS]


def identity_key(sig: dict) -> tuple:
    return (sig["agent"], sig["competitor"], sig["type"], normalize_headline(sig["payload"]))


def compute_delta(prev_signals: list[dict], curr_signals: list[dict]) -> list[dict]:
    """Signals in curr with no identity-key match in prev, shaped for DeltaSignal."""
    seen = {identity_key(s) for s in prev_signals}
    new = []
    for s in curr_signals:
        if identity_key(s) in seen:
            continue
        new.append({
            "agent": s["agent"],
            "competitor": s["competitor"],
            "type": s["type"],
            "headline": _raw_headline(s["payload"]),
            "evidence_ids": s.get("evidence_ids") or [],
            "claim_ref": f"{s['type']}:{s['competitor'].lower()}",
        })
    return new
