"""Markdown export for competitive reports.

Owner: Dharvi. Converts a CompetitiveReport dict (see app/models.py, the
report.jsonb column) into clean CommonMark — no HTML, deterministic ordering
— for GET /api/v1/reports/{run_id}/export?format=md.

report is the CompetitiveReport shape as persisted:
  company: str · threat_level: LOW|MEDIUM|HIGH|CRITICAL · executive_summary: str
  swot: {strengths,weaknesses,opportunities,threats: [str]}
  sentiment: {competitor -> {score, label}}
  head_to_head: [{metric, you, rivals: {competitor -> {value, claim_ref, source_date}}}]
  opportunities: [{text, evidence_ids, claim_ref}]
  recommendations: [{action, rationale, confidence, evidence_ids, claim_ref}]
  low_signal_findings: [str] · analysis_date: str

There is no raw per-competitor blob on the report — "competitor rollups" are
derived here from `sentiment` and `head_to_head`, the two fields that actually
carry per-rival data.
"""
from typing import Callable

EvidenceLookup = Callable[[list[str]], list[dict]]

_THREAT_BADGE = {
    "CRITICAL": "🔴 CRITICAL",
    "HIGH": "🟠 HIGH",
    "MEDIUM": "🟡 MEDIUM",
    "LOW": "🟢 LOW",
}


def report_to_markdown(report: dict, evidence_lookup: EvidenceLookup) -> str:
    """Render one CompetitiveReport dict as clean CommonMark markdown.

    evidence_lookup(evidence_ids) -> [{source_name, url, snippet}, ...] is
    called once per opportunity/recommendation to resolve its citations
    (e.g. repository.get_evidence_by_ids).
    """
    company = report.get("company", "Unknown")
    threat = report.get("threat_level", "")

    lines: list[str] = [
        f"# {company} — Competitive Threat Report",
        "",
        f"**Threat Level:** {_THREAT_BADGE.get(threat, threat)}",
        f"**Report Date:** {report.get('analysis_date', '')}",
        "",
        "## Executive Summary",
        "",
        report.get("executive_summary") or "_No summary available._",
        "",
    ]
    lines += _rival_rollups(report)
    lines += _swot(report.get("swot") or {})
    lines += _cited_section(
        "Opportunities", report.get("opportunities") or [], _opportunity_heading, evidence_lookup
    )
    lines += _cited_section(
        "Recommendations", report.get("recommendations") or [], _recommendation_heading, evidence_lookup
    )
    lines += _low_signal(report.get("low_signal_findings") or [])

    return "\n".join(lines).rstrip() + "\n"


def _rival_rollups(report: dict) -> list[str]:
    """One section per rival named in sentiment or head_to_head — the only
    two fields that actually carry per-competitor data on this report shape.
    """
    sentiment: dict = report.get("sentiment") or {}
    h2h: list[dict] = report.get("head_to_head") or []
    rivals = sorted(set(sentiment) | {name for row in h2h for name in (row.get("rivals") or {})})
    if not rivals:
        return []

    lines = ["## Competitor Rollups", ""]
    for rival in rivals:
        lines.append(f"### {rival}")
        s = sentiment.get(rival)
        if s:
            lines.append(f"- **Sentiment:** {s.get('label', 'NEUTRAL')} ({s.get('score', 0):.0%})")
        for row in h2h:
            cell = (row.get("rivals") or {}).get(rival)
            if cell is None:
                continue
            date_suffix = f" (as of {cell['source_date']})" if cell.get("source_date") else ""
            lines.append(f"- **{row.get('metric', '')}:** {cell.get('value', '')}{date_suffix}")
        lines.append("")
    return lines


def _swot(swot: dict) -> list[str]:
    lines = ["## SWOT Analysis", ""]
    for key, heading in (
        ("strengths", "Strengths"),
        ("weaknesses", "Weaknesses"),
        ("opportunities", "Opportunities"),
        ("threats", "Threats"),
    ):
        lines.append(f"### {heading}")
        items = swot.get(key) or []
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append(f"- _No {heading.lower()} identified._")
        lines.append("")
    return lines


def _opportunity_heading(item: dict) -> tuple[str, str]:
    return item.get("text", "Untitled opportunity"), ""


def _recommendation_heading(item: dict) -> tuple[str, str]:
    action = item.get("action", "Untitled recommendation")
    confidence = item.get("confidence")
    suffix = f" (confidence: {confidence:.0%})" if isinstance(confidence, (int, float)) else ""
    return f"{action}{suffix}", item.get("rationale", "")


def _cited_section(
    title: str, items: list[dict], heading_fn: Callable[[dict], tuple[str, str]], evidence_lookup: EvidenceLookup
) -> list[str]:
    """Render items in the report's own order (already the strategist's
    canonical/priority order — stable across renders, so left as-is)."""
    lines = [f"## {title}", ""]
    if not items:
        lines.append(f"_No {title.lower()} identified._")
        lines.append("")
        return lines
    for item in items:
        heading, body = heading_fn(item)
        lines.append(f"### {heading}")
        if body:
            lines.append("")
            lines.append(body)
        lines.append("")
        evidence_ids = item.get("evidence_ids") or []
        if evidence_ids:
            lines.append("**Evidence:**")
            for ev in evidence_lookup(evidence_ids):
                source = ev.get("source_name", "Unknown source")
                url = ev.get("url", "")
                lines.append(f"- {source} — {url}" if url else f"- {source}")
            lines.append("")
    return lines


def _low_signal(findings: list[str]) -> list[str]:
    if not findings:
        return []
    lines = ["## Low-Signal Findings", ""]
    lines.extend(f"- {item}" for item in findings)
    lines.append("")
    return lines
