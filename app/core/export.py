"""Report -> Markdown export. Owner: Dharvi.

Renders a CompetitiveReport as clean, stable CommonMark for GET
/reports/{run_id}/export?format=md — title/threat/date, executive summary,
SWOT, head-to-head, opportunities, and recommendations (each with its
code-computed confidence and, when available, an indented Evidence list).

Pure and deterministic: same report -> byte-identical markdown, so the export
can be cached in reports.md_export.
"""
from __future__ import annotations

from ..models import CompetitiveReport, EvidenceRow


def report_to_markdown(report: CompetitiveReport,
                       evidence_lookup: dict[str, list[EvidenceRow]] | None = None) -> str:
    """Render `report` as markdown. `evidence_lookup` maps claim_ref -> evidence
    rows; when provided, each opportunity/recommendation gets an Evidence sublist."""
    lookup = evidence_lookup or {}
    out: list[str] = []

    out.append(f"# Competitive Analysis — {report.company}")
    out.append("")
    out.append(f"**Threat level:** {report.threat_level}  ")
    out.append(f"**Analysis date:** {report.analysis_date}")
    out.append("")

    out.append("## Executive Summary")
    out.append(report.executive_summary.strip() or "_No summary available._")
    out.append("")

    out.append("## SWOT")
    for label, items in (("Strengths", report.swot.strengths),
                         ("Weaknesses", report.swot.weaknesses),
                         ("Opportunities", report.swot.opportunities),
                         ("Threats", report.swot.threats)):
        out.append(f"**{label}**")
        out.extend(f"- {i}" for i in items) if items else out.append("- _none_")
        out.append("")

    if report.head_to_head:
        out.append("## Head-to-Head")
        rivals = sorted({r for row in report.head_to_head for r in row.rivals})
        out.append("| Metric | You | " + " | ".join(rivals) + " |")
        out.append("|" + "---|" * (2 + len(rivals)))
        for row in report.head_to_head:
            cells = [row.rivals[r].value if r in row.rivals else "—" for r in rivals]
            out.append(f"| {row.metric} | {row.you} | " + " | ".join(cells) + " |")
        out.append("")

    if report.opportunities:
        out.append("## Opportunities")
        for opp in report.opportunities:
            out.append(f"- {opp.text}")
            out.extend(_evidence_lines(opp.claim_ref, lookup))
        out.append("")

    if report.recommendations:
        out.append("## Recommendations")
        for rec in report.recommendations:
            out.append(f"### {rec.action}")
            out.append(f"{rec.rationale}  ")
            out.append(f"_Confidence: {rec.confidence:.2f}_")
            out.extend(_evidence_lines(rec.claim_ref, lookup))
            out.append("")

    if report.low_signal_findings:
        out.append("## Low-Signal Findings")
        out.extend(f"- {f}" for f in report.low_signal_findings)
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def _evidence_lines(claim_ref: str, lookup: dict[str, list[EvidenceRow]]) -> list[str]:
    rows = lookup.get(claim_ref) or []
    if not rows:
        return []
    lines = ["  - Evidence:"]
    lines.extend(f"    - {r.source_name} — {r.url}" for r in rows)
    return lines
