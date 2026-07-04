"""Per-competitor intel cache — FEATURE-FLAGGED, OFF by default.

Goal (UI team's design): re-analyzing a company with an overlapping rival set
should only call the gathering agents for the NEWLY-added rivals. Each rival's
agent output (news / product / review) is cached by rival name; on the next run,
a rival we already have fresh intel for is reused with ZERO agent calls. Fewer
LLM calls => fewer 429 rate-limit failures => more reliable reports.

Safety: this can only ever SAVE work, never break the pipeline.
  - Disabled unless COMPETITOR_INTEL_CACHE=1.
  - Any cache miss OR error falls back to running the agent for that rival.
  - merge + strategist still run every time, so evidence and the report are always
    freshly built for the CURRENT selection — only the raw gather step is reused.
  - Locally (no Redis/Postgres) the shared cache is a no-op, so nothing is reused
    and behaviour is identical to the flag being off.

Staleness: reused intel can be up to COMPETITOR_INTEL_TTL old (24h default).
"""
from __future__ import annotations

from typing import Callable

from app.core import cache, config


def enabled() -> bool:
    return bool(getattr(config, "COMPETITOR_INTEL_CACHE", False))


def _key(kind: str, competitor: str) -> str:
    return cache.make_cache_key(f"intel:{kind}:{competitor.strip().lower()}")


def _get(kind: str, competitor: str) -> dict | None:
    try:
        return cache.cache_get(_key(kind, competitor))
    except Exception:  # noqa: BLE001 — a cache read must never break gathering
        return None


def _put(kind: str, competitor: str, payload: dict) -> None:
    try:
        cache.cache_set(_key(kind, competitor), payload,
                        ttl=getattr(config, "COMPETITOR_INTEL_TTL", 86400))
    except Exception:  # noqa: BLE001 — a cache write must never break gathering
        pass


def _as_dict(item) -> dict:
    return item.model_dump() if hasattr(item, "model_dump") else item


def gather(kind: str, names: list[str], run_subset: Callable[[list[str]], list], emit) -> list:
    """Return one intel item per name (order preserved), reusing cached items and
    calling `run_subset` only for the names without fresh cache.

    `run_subset(subset)` runs the real agent for a subset of rival names and returns
    one item per name in order (the agents' frozen contract). When the flag is off,
    this is a straight pass-through — identical to the old behaviour.
    """
    if not enabled() or not names:
        return run_subset(names)

    reused: dict[str, dict] = {}
    to_gather: list[str] = []
    for name in names:
        hit = _get(kind, name)
        if hit is not None:
            reused[name] = hit
            emit(kind, f"{name} · reused cached {kind} intel (no agent call)")
        else:
            to_gather.append(name)

    fresh = run_subset(to_gather) if to_gather else []
    fresh_by_name: dict[str, object] = {}
    # Agents return one item per input name, in order (frozen contract), so zip
    # aligns each fresh item with its rival. A short return just means those trailing
    # names fall through as missing and are skipped below — never mismatched.
    for name, item in zip(to_gather, fresh):
        fresh_by_name[name] = item
        _put(kind, name, _as_dict(item))

    out: list = []
    for name in names:
        item = reused[name] if name in reused else fresh_by_name.get(name)
        if item is not None:
            out.append(item)
    return out
