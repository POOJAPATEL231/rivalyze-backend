"""Merge — the deterministic code node between the gathering agents and the
strategist. NOT an LLM agent.

Owner: Gati. It turns the three parallel agent outputs (news ‖ product ‖ review)
into ONE cited evidence graph:
  1. every inner finding becomes an EvidenceRow (id "ev-"+hex, snippet ≤280) and
     is persisted best-effort via repository.save_evidence;
  2. findings roll up into typed Signals (with their evidence_ids) — persisted
     via repository.save_signal;
  3. per-competitor rollups (pricing / features / complaints / sentiment / news)
     carry the evidence_ids so the strategist can cite them and code can recompute
     confidence from the same graph.

Contract with the orchestrator: it is injected as the "merge" node
(NodeFn: (state, emit) -> dict) and returns {"unified": UnifiedSignals(...)}.
It NEVER raises — a malformed item becomes a low_signal finding, not a crash.
Persistence failures (no DATABASE_URL in MOCK_MODE) are swallowed so a full run
still completes offline.

The evidence index (id -> {agent, competitor, type, claim_ref, url, source_name})
rides in per_competitor["__evidence_index__"] so the strategist can validate
citations and recompute confidence without re-reading the database.
"""
from __future__ import annotations

import logging
import re
import uuid

from ..db import repository
from ..models import (
    EvidenceRow,
    NewsSignals,
    ProductIntel,
    SentimentIntel,
    Signal,
    UnifiedSignals,
)

logger = logging.getLogger(__name__)

EVIDENCE_INDEX_KEY = "__evidence_index__"
_SNIPPET_CAP = 280


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "rival"


def _ev_id() -> str:
    return "ev-" + uuid.uuid4().hex[:8]


def _source_name_from_url(url: str, fallback: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else (fallback or "source")


def _as(model, item):
    """Coerce a state item (dict OR already-validated model) to its model type."""
    if isinstance(item, model):
        return item
    return model.model_validate(item)


def merge_node(state: dict, emit) -> dict:
    """Fuse news/product/review into a cited UnifiedSignals. Never raises."""
    run_id = state.get("run_id", "")
    signals: list[Signal] = []
    evidence_index: dict[str, dict] = {}
    per_competitor: dict[str, dict] = {}
    findings: list[str] = []
    rows_saved = 0

    def bucket(comp: str) -> dict:
        return per_competitor.setdefault(comp, {
            "pricing_tiers": [], "recent_features": [], "advantages": [],
            "complaints": [], "opportunity_gaps": [], "sentiment": "NEUTRAL",
            "news": [], "evidence_ids": [],
        })

    def add_evidence(run_id: str, claim_ref: str, source_type: str,
                     url: str, snippet: str, agent: str, competitor: str,
                     source_date: str = "") -> str | None:
        eid = _ev_id()
        try:
            row = EvidenceRow(
                id=eid, run_id=run_id or "unknown", claim_ref=claim_ref,
                source_type=source_type, source_name=_source_name_from_url(url, agent),
                url=url or "", snippet=(snippet or "")[:_SNIPPET_CAP],
                source_date=source_date or "", agent=agent,
            )
        except Exception as exc:  # noqa: BLE001 — a bad item costs the item, not the run
            findings.append(f"merge: dropped malformed {agent} evidence for {competitor} ({type(exc).__name__})")
            return None
        evidence_index[eid] = {
            "agent": agent, "competitor": competitor, "type": source_type,
            "claim_ref": claim_ref, "url": row.url, "source_name": row.source_name,
        }
        try:
            repository.save_evidence(row.model_dump())
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort (MOCK/no-DB)
            logger.debug("merge: save_evidence skipped (%s)", type(exc).__name__)
        bucket(competitor)["evidence_ids"].append(eid)
        return eid

    def add_signal(agent: str, competitor: str, sig_type: str,
                   payload: dict, evidence_ids: list[str]) -> None:
        try:
            sig = Signal(run_id=run_id or "unknown", agent=agent, competitor=competitor,
                         type=sig_type, payload=payload, evidence_ids=evidence_ids)
        except Exception as exc:  # noqa: BLE001
            findings.append(f"merge: dropped malformed {agent} signal for {competitor} ({type(exc).__name__})")
            return
        signals.append(sig)
        try:
            repository.save_signal(sig.model_dump())
        except Exception as exc:  # noqa: BLE001
            logger.debug("merge: save_signal skipped (%s)", type(exc).__name__)

    # ---- news ----
    for raw in state.get("news_results", []):
        try:
            ns = _as(NewsSignals, raw)
        except Exception as exc:  # noqa: BLE001
            findings.append(f"merge: unparseable news item ({type(exc).__name__})")
            continue
        if ns.low_signal or not ns.items:
            findings.append(f"news: low signal for {ns.competitor}")
        claim = f"news:{_slug(ns.competitor)}"
        for it in ns.items:
            eid = add_evidence(run_id, claim, "news", it.source_url,
                               f"{it.event} — {it.impact}", "news", ns.competitor, it.date)
            b = bucket(ns.competitor)
            b["news"].append({"event": it.event, "impact": it.impact, "url": it.source_url})
            add_signal("news", ns.competitor, "launch",
                       {"event": it.event, "impact": it.impact}, [eid] if eid else [])

    # ---- product ----
    for raw in state.get("product_results", []):
        try:
            pi = _as(ProductIntel, raw)
        except Exception as exc:  # noqa: BLE001
            findings.append(f"merge: unparseable product item ({type(exc).__name__})")
            continue
        if pi.low_signal:
            findings.append(f"product: low signal for {pi.competitor}")
        b = bucket(pi.competitor)
        b["pricing_tiers"].extend(pi.pricing_tiers)
        b["recent_features"].extend(pi.recent_features)
        b["advantages"].extend(pi.advantages)
        claim = f"pricing:{_slug(pi.competitor)}"
        eids: list[str] = []
        for url in pi.sources:
            eid = add_evidence(run_id, claim, "pricing", url,
                               "; ".join(pi.pricing_tiers[:2]) or pi.positioning,
                               "product", pi.competitor)
            if eid:
                eids.append(eid)
        add_signal("product", pi.competitor, "pricing",
                   {"pricing_tiers": pi.pricing_tiers, "positioning": pi.positioning}, eids)

    # ---- review ----
    for raw in state.get("review_results", []):
        try:
            si = _as(SentimentIntel, raw)
        except Exception as exc:  # noqa: BLE001
            findings.append(f"merge: unparseable review item ({type(exc).__name__})")
            continue
        if si.low_signal:
            findings.append(f"review: low signal for {si.competitor}")
        b = bucket(si.competitor)
        b["complaints"].extend(si.top_complaints)
        b["opportunity_gaps"].extend(si.opportunity_gaps)
        b["sentiment"] = si.overall_sentiment
        claim = f"review:{_slug(si.competitor)}"
        eids = []
        for url in si.sources:
            eid = add_evidence(run_id, claim, "review", url,
                               "; ".join(si.top_complaints[:2]), "review", si.competitor)
            if eid:
                eids.append(eid)
        add_signal("review", si.competitor, "complaint",
                   {"complaints": si.top_complaints, "sentiment": si.overall_sentiment}, eids)

    rows_saved = len(evidence_index)
    emit("merge", f"fused {len(signals)} signals · {rows_saved} evidence rows · "
                  f"{len(per_competitor)} rivals")

    per_competitor[EVIDENCE_INDEX_KEY] = evidence_index
    unified = UnifiedSignals(
        signals=signals,
        per_competitor=per_competitor,
        low_signal_findings=findings,
    )
    return {"unified": unified, "low_signal_findings": findings}
