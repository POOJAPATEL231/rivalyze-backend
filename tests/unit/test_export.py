"""Unit tests for app/core/export.report_to_markdown — a fixture report dict
shaped exactly like CompetitiveReport (app/models.py), no database needed.
"""
from app.core.export import report_to_markdown

FIXTURE_REPORT = {
    "company": "Notion",
    "threat_level": "HIGH",
    "executive_summary": "Coda and Airtable are closing the AI gap fast.",
    "swot": {
        "strengths": ["Strong brand", "Large user base"],
        "weaknesses": ["Slow AI rollout"],
        "opportunities": ["Bundle AI writing assistant"],
        "threats": ["Coda AI launch"],
    },
    "sentiment": {
        "Coda": {"score": 0.72, "label": "POSITIVE"},
        "Airtable": {"score": 0.41, "label": "NEUTRAL"},
    },
    "head_to_head": [
        {
            "metric": "Starting price",
            "you": "$8/user/mo",
            "rivals": {
                "Coda": {"value": "$10/user/mo", "claim_ref": "pricing:coda", "source_date": "2026-06-01"},
                "Airtable": {"value": "$12/user/mo", "claim_ref": "pricing:airtable", "source_date": None},
            },
        },
    ],
    "opportunities": [
        {
            "text": "Bundle an AI writing assistant into the free tier",
            "evidence_ids": ["ev-1"],
            "claim_ref": "opp:bundle-ai",
        },
    ],
    "recommendations": [
        {
            "action": "Ship AI-assisted docs by Q3",
            "rationale": "Coda's AI launch is drawing enterprise attention.",
            "confidence": 0.83,
            "evidence_ids": ["ev-1", "ev-2"],
            "claim_ref": "rec:bundle-ai",
        },
    ],
    "low_signal_findings": ["Airtable's roadmap is not publicly documented."],
    "analysis_date": "2026-07-04T14:02:11+00:00",
}

_EVIDENCE_CATALOG = {
    "ev-1": {"source_name": "TechCrunch", "url": "https://techcrunch.com/coda-ai", "snippet": "Coda launched AI."},
    "ev-2": {"source_name": "G2 Reviews", "url": "https://g2.com/coda", "snippet": "Users like the AI feature."},
}


def _evidence_lookup(ids: list[str]) -> list[dict]:
    return [_EVIDENCE_CATALOG[i] for i in ids if i in _EVIDENCE_CATALOG]


def test_header_has_company_threat_and_date():
    md = report_to_markdown(FIXTURE_REPORT, _evidence_lookup)
    assert md.startswith("# Notion — Competitive Threat Report")
    assert "HIGH" in md
    assert "2026-07-04T14:02:11+00:00" in md


def test_executive_summary_included():
    md = report_to_markdown(FIXTURE_REPORT, _evidence_lookup)
    assert "Coda and Airtable are closing the AI gap fast." in md


def test_rival_rollups_include_sentiment_and_head_to_head():
    md = report_to_markdown(FIXTURE_REPORT, _evidence_lookup)
    assert "### Airtable" in md
    assert "### Coda" in md
    assert "POSITIVE (72%)" in md
    assert "Starting price:** $10/user/mo (as of 2026-06-01)" in md
    assert "$12/user/mo" in md


def test_rival_order_is_alphabetical_and_deterministic():
    md1 = report_to_markdown(FIXTURE_REPORT, _evidence_lookup)
    md2 = report_to_markdown(FIXTURE_REPORT, _evidence_lookup)
    assert md1 == md2
    assert md1.index("### Airtable") < md1.index("### Coda")


def test_swot_sections_present():
    md = report_to_markdown(FIXTURE_REPORT, _evidence_lookup)
    assert "### Strengths" in md and "Strong brand" in md
    assert "### Weaknesses" in md and "Slow AI rollout" in md
    assert "### Threats" in md and "Coda AI launch" in md


def test_opportunity_and_recommendation_have_evidence_lists():
    md = report_to_markdown(FIXTURE_REPORT, _evidence_lookup)
    assert "### Bundle an AI writing assistant into the free tier" in md
    assert "### Ship AI-assisted docs by Q3 (confidence: 83%)" in md
    assert "TechCrunch — https://techcrunch.com/coda-ai" in md
    assert "G2 Reviews — https://g2.com/coda" in md


def test_low_signal_findings_included():
    md = report_to_markdown(FIXTURE_REPORT, _evidence_lookup)
    assert "Airtable's roadmap is not publicly documented." in md


def test_no_html_in_output():
    md = report_to_markdown(FIXTURE_REPORT, _evidence_lookup)
    assert "<" not in md


def test_handles_missing_optional_sections_gracefully():
    minimal = {
        "company": "Acme",
        "threat_level": "LOW",
        "executive_summary": "",
        "swot": {},
        "sentiment": {},
        "head_to_head": [],
        "opportunities": [],
        "recommendations": [],
        "low_signal_findings": [],
        "analysis_date": "2026-01-01T00:00:00+00:00",
    }
    md = report_to_markdown(minimal, lambda ids: [])
    assert "_No summary available._" in md
    assert "_No opportunities identified._" in md
    assert "_No recommendations identified._" in md
    assert "## Competitor Rollups" not in md
    assert "## Low-Signal Findings" not in md
