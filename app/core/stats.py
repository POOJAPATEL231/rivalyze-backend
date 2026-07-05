"""Stats Node — deterministic "By the Numbers" (NO AI, pure aggregation).

Every value is a COUNT / GROUP BY over the evidence + signals rows already produced
this run, so it CANNOT hallucinate: a QA person counting the same rows gets the same
figure. Runs at report assembly (the strategist), once evidence, signals, sentiment
and the final recommendations all exist. The caller wraps it so any failure degrades
to `stats=None` and the report is unaffected (the field is additive).

Honesty rules (Rivalyze_Stats_Node.md):
  - only count/aggregate rows that exist — never estimate market share, TAM, growth,
    valuation, or anything not literally in the rows;
  - every rate guards division by zero (returns None when the denominator is 0);
  - an empty/thin run yields small numbers (0 / None), never an error, never a blank.
"""
from collections import Counter
from datetime import date, datetime
from statistics import mean

import re

# The canonical evidence source types (EvidenceRow.source_type Literal). Pre-seeded
# into the breakdown so the donut has stable slices; sum still == evidence_count.
_SOURCE_TYPES = ("news", "pricing", "review", "web", "document")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y")


def _field(obj, name, default=None):
    """Read `name` from either a dict or an object — lets compute_stats accept the
    in-memory evidence_index dicts (keys) AND typed EvidenceRow/Signal objects (attrs)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", str(name).lower()).strip("-")


def _ev_source_type(e) -> str:
    # evidence_index dicts carry it as "type"; EvidenceRow carries "source_type".
    return _field(e, "source_type") or _field(e, "type") or "web"


def _ev_claim_ref(e) -> str:
    return _field(e, "claim_ref") or ""


def _ev_competitor(e, competitors: list[str]):
    """Which confirmed competitor an evidence row belongs to. Prefer the explicit
    `competitor` field (on the evidence index); fall back to matching the claim_ref
    suffix against each competitor's slug so EvidenceRow objects (no competitor
    field) still map."""
    c = _field(e, "competitor")
    if c:
        return c
    cr = _ev_claim_ref(e).lower()
    for name in competitors:
        if cr.endswith(_slug(name)) or cr.endswith(str(name).lower()):
            return name
    return None


def _parse_date(raw) -> date | None:
    raw = (raw or "").strip() if isinstance(raw, str) else ""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _min_age_days(evidence, today: date | None = None) -> int | None:
    """Age (days) of the freshest dated evidence row, or None if nothing has a
    parseable date. Free-text dates that don't parse are simply ignored — never
    an error."""
    today = today or datetime.now().date()
    ages = []
    for e in evidence:
        d = _parse_date(_field(e, "source_date"))
        if d is not None:
            age = (today - d).days
            if age >= 0:
                ages.append(age)
    return min(ages) if ages else None


def _tally(values) -> dict:
    return dict(Counter(v for v in values if v))


def _count_rivals_with(signals, sig_type: str, competitors: list[str]) -> int:
    """Distinct CONFIRMED competitors with >=1 signal of `sig_type` (case-insensitive
    match to the confirmed set — so the count can never exceed competitors_analyzed)."""
    confirmed = {str(c).lower() for c in competitors}
    rivals = {_field(s, "competitor") for s in signals if _field(s, "type") == sig_type}
    return len({r for r in rivals if r and str(r).lower() in confirmed})


def _sentiment_buckets(sentiment: dict) -> dict:
    buckets = {"POSITIVE": 0, "NEUTRAL": 0, "NEGATIVE": 0}
    for v in (sentiment or {}).values():
        label = _field(v, "label")
        if label in buckets:
            buckets[label] += 1
    return buckets


def _corroboration_pct(evidence) -> int | None:
    """Percent of claim_refs backed by 2+ independent evidence rows — the honesty
    stat. None when there are no claims (no division by zero)."""
    groups = Counter(_ev_claim_ref(e) for e in evidence if _ev_claim_ref(e))
    total = len(groups)
    if total == 0:
        return None
    corroborated = sum(1 for n in groups.values() if n >= 2)
    return round(100 * corroborated / total)


def compute_stats(evidence, signals, competitors, sentiment, recommendations) -> dict:
    """Deterministic aggregates for the report's "By the numbers" strip.

    Inputs are rows/objects already produced this run:
      evidence        — evidence_index values (or an EvidenceRow list)
      signals         — Signal list
      competitors     — confirmed rival names
      sentiment       — report.sentiment {rival: SentimentScore}
      recommendations — the FINAL recommendations (for avg confidence)

    Every number is a count/group-by; rates return None on an empty denominator.
    """
    evidence = list(evidence or [])
    signals = list(signals or [])
    competitors = list(competitors or [])

    # source-type donut — pre-seed the canonical slices, then count. Each evidence
    # row contributes exactly one +1, so the values sum to evidence_count (TC-ST02).
    breakdown = {t: 0 for t in _SOURCE_TYPES}
    for e in evidence:
        st = _ev_source_type(e)
        breakdown[st] = breakdown.get(st, 0) + 1

    # sources per competitor — bucket by the confirmed name (case-insensitive).
    lower_map = {str(c).lower(): c for c in competitors}
    per_comp = {c: 0 for c in competitors}
    for e in evidence:
        c = _ev_competitor(e, competitors)
        if c and str(c).lower() in lower_map:
            per_comp[lower_map[str(c).lower()]] += 1

    rec_conf = [_field(r, "confidence") for r in recommendations or []]
    rec_conf = [c for c in rec_conf if isinstance(c, (int, float))]

    return {
        "evidence_count": len(evidence),
        "competitors_analyzed": len(competitors),
        "sources_per_competitor": per_comp,
        "source_type_breakdown": breakdown,
        "signals_by_type": _tally(_field(s, "type") for s in signals),
        "competitors_with_complaints": _count_rivals_with(signals, "complaint", competitors),
        "sentiment_spread": _sentiment_buckets(sentiment),
        "avg_confidence": round(mean(rec_conf), 2) if rec_conf else None,
        "freshest_signal_days": _min_age_days(evidence),
        "corroboration_rate": _corroboration_pct(evidence),
    }
