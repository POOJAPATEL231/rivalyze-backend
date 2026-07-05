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
from urllib.parse import urlparse

EvidenceLookup = Callable[[list[str]], list[dict]]

_THREAT_BADGE = {
    "CRITICAL": "🔴 CRITICAL",
    "HIGH": "🟠 HIGH",
    "MEDIUM": "🟡 MEDIUM",
    "LOW": "🟢 LOW",
}


def report_to_markdown(report: dict, evidence_lookup: EvidenceLookup,
                       all_evidence: list[dict] | None = None) -> str:
    """Render one CompetitiveReport dict as clean CommonMark markdown — a complete,
    lossless view of the report JSON so the export carries the SAME data as
    GET /reports/{id}.

    evidence_lookup(evidence_ids) -> [{source_name, url, snippet}, ...] resolves an
    opportunity/recommendation's inline citations (e.g. repository.get_evidence_by_ids).

    all_evidence is EVERY evidence row for the run (repository.get_all_evidence_for_run):
    it feeds the full "Sources" appendix so the markdown lists all distinct sources,
    not just the ids that opportunities/recommendations cited. None -> the appendix is
    skipped (older callers keep working unchanged).
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
    lines += _verdict(report.get("verdict") or {})
    lines += _rival_rollups(report)
    lines += _swot(report.get("swot") or {})
    lines += _cited_section(
        "Opportunities", report.get("opportunities") or [], _opportunity_heading, evidence_lookup
    )
    lines += _cited_section(
        "Recommendations", report.get("recommendations") or [], _recommendation_heading, evidence_lookup
    )
    lines += _stats(report.get("stats") or {})
    lines += _suggested_questions(report.get("suggested_questions") or [])
    lines += _low_signal(report.get("low_signal_findings") or [])
    lines += _sources(all_evidence or [])

    return "\n".join(lines).rstrip() + "\n"


def _verdict(verdict: dict) -> list[str]:
    """The bottom-line "Verdict" block — same data the Side-by-side view shows,
    rendered as markdown. Additive: an older report with no verdict renders nothing.
    Guarded field-by-field so a malformed persisted value can't 500 the export."""
    if not verdict:
        return []
    lines = ["## Verdict", ""]
    if verdict.get("headline"):
        lines += [f"**{verdict['headline']}**", ""]
    if verdict.get("summary"):
        lines += [verdict["summary"], ""]
    bullets: list[str] = []
    if verdict.get("biggest_threat"):
        bullets.append(f"- **Biggest threat:** {verdict['biggest_threat']}")
    if verdict.get("openings"):
        bullets.append(f"- **Openings:** {', '.join(verdict['openings'])}")
    if verdict.get("you_lead"):
        bullets.append(f"- **You lead on:** {', '.join(verdict['you_lead'])}")
    if verdict.get("top_move"):
        bullets.append(f"- **Top move:** {verdict['top_move']}")
    if bullets:
        lines += bullets + [""]
    if verdict.get("confidence_note"):
        lines += [f"_{verdict['confidence_note']}_", ""]
    return lines


def _stats(stats: dict) -> list[str]:
    """The "By the Numbers" strip (ReportStats) — deterministic counts already on the
    report JSON. Rendered so the export carries the same data as GET /reports/{id}."""
    if not stats:
        return []
    lines = ["## By the Numbers", ""]
    for label, key in (
        ("Evidence collected", "evidence_count"),
        ("Competitors analyzed", "competitors_analyzed"),
        ("Distinct sources", "distinct_sources"),
        ("Competitors with complaints", "competitors_with_complaints"),
        ("Uncorroborated claims", "uncorroborated_claims"),
    ):
        val = stats.get(key)
        if val is not None:
            lines.append(f"- **{label}:** {val}")
    if stats.get("corroboration_rate") is not None:
        lines.append(f"- **Corroboration rate:** {stats['corroboration_rate']}%")
    if stats.get("freshest_signal_days") is not None:
        lines.append(f"- **Freshest signal:** {stats['freshest_signal_days']} days old")
    avg = stats.get("avg_confidence")
    if isinstance(avg, (int, float)):
        lines.append(f"- **Avg. recommendation confidence:** {avg:.0%}")
    lines.append("")
    return lines


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
            # report is the raw report.jsonb dict, not a validated CompetitiveReport —
            # guard score the same way _recommendation_heading guards confidence, so a
            # malformed persisted value can't 500 the export.
            score = s.get("score")
            pct = f"{score:.0%}" if isinstance(score, (int, float)) else "n/a"
            lines.append(f"- **Sentiment:** {s.get('label', 'NEUTRAL')} ({pct})")
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


def _suggested_questions(questions: list[str]) -> list[str]:
    """Render the report's follow-up questions (present on newer reports)."""
    if not questions:
        return []
    lines = ["## Suggested Questions", ""]
    lines.extend(f"- {q}" for q in questions if isinstance(q, str) and q.strip())
    lines.append("")
    return lines


def _domain(url: str) -> str:
    """Source domain (netloc) — same extraction app/core/stats.py uses for
    distinct_sources, so this appendix's domain count matches that stat."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        return urlparse(url if "://" in url else "//" + url).netloc.lower()
    except ValueError:
        return ""


def _sources(all_evidence: list[dict]) -> list[str]:
    """Full "Sources" appendix — EVERY source gathered for the run, grouped by
    domain. Closes the gap the inline "Evidence:" lists left (those show only the ids
    opportunities/recommendations cited). The domain count matches
    stats.distinct_sources and the item count matches stats.evidence_count, so the
    markdown reconciles with the JSON's numbers while still listing every URL."""
    if not all_evidence:
        return []
    by_domain: dict[str, list[tuple[str, str]]] = {}
    seen_urls: set[str] = set()
    for e in all_evidence:
        url = (e.get("url") or "").strip()
        name = (e.get("source_name") or "").strip() or "Unknown source"
        dom = _domain(url) or name
        key = url or f"{dom}:{name}"
        if key in seen_urls:
            continue
        seen_urls.add(key)
        by_domain.setdefault(dom, []).append((name, url))
    lines = ["## Sources", "",
             f"_{len(by_domain)} distinct source domains · {len(all_evidence)} evidence items._", ""]
    for dom in sorted(by_domain):
        lines.append(f"### {dom}")
        for name, url in by_domain[dom]:
            lines.append(f"- {name} — {url}" if url else f"- {name}")
        lines.append("")
    return lines


def _low_signal(findings: list[str]) -> list[str]:
    if not findings:
        return []
    lines = ["## Low-Signal Findings", ""]
    lines.extend(f"- {item}" for item in findings)
    lines.append("")
    return lines
