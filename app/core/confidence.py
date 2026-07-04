"""Confidence — the ONE code-computed number behind every recommendation.

Owner: Gati. Pure function, zero I/O, zero LLM. The strategist agent is handed
this via `strategist_node(run_fn, confidence_fn)` and recomputes every
Recommendation/Opportunity confidence from its CITED evidence — the model's own
asserted numbers are discarded (honesty rule: "no rendered number is uncomputed").

Formula (dev-pack §confidence):
    0.25 + 0.12·min(sources,5) + 0.15·(agreeing/max(sources,1)) + 0.10·min(agents,3)
clamped to [0.05, 0.95], rounded to 2dp.

The full 0.05–0.95 range is deliberate: a single source from a single agent must
read visibly low, and even a wall of agreeing evidence never asserts certainty.

NOTE (spec ambiguity, flagged for Gati): the dev-pack comment says (1,1,1) should
land "~0.42", but this formula yields 0.62 for (1,1,1). The formula is implemented
as written (it is the authoritative arithmetic); tests assert the ORDERING/bounds
the spec cares about (a lone source is low and strictly below a corroborated one,
(5,5,3) sits high in (0.8, 0.95]) rather than pinning the disputed constant.
Resolve the intended coefficient before hard-coding an exact expected value.
"""
from __future__ import annotations

_FLOOR = 0.05
_CEIL = 0.95


def confidence(source_count: int, agreeing: int, corroborating_agents: int) -> float:
    """Compute a claim's confidence from its evidence graph.

    Args:
        source_count: distinct sources (URLs) backing the claim.
        agreeing: sources that corroborate the same finding (the largest set
            pointing the same way); bounded by source_count in practice.
        corroborating_agents: distinct agents (news/product/review) that surfaced
            evidence for the claim.

    Returns:
        A float in [0.05, 0.95], rounded to 2 decimal places. Inputs are treated
        as non-negative (clamped at 0) so a malformed count never produces a
        nonsense or out-of-range score.
    """
    s = max(0, int(source_count))
    a = max(0, int(agreeing))
    g = max(0, int(corroborating_agents))

    raw = (
        0.25
        + 0.12 * min(s, 5)
        + 0.15 * (a / max(s, 1))
        + 0.10 * min(g, 3)
    )
    return round(max(_FLOOR, min(_CEIL, raw)), 2)
