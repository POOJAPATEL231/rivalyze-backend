"""Deterministic suggested chat questions for a completed report.

No LLM call — the questions are templated from the report/competitors the
run already produced, so this is free and instant (GET /reports/{run_id}
can populate it on every request, not just once at analysis time).
"""
from ..models import CompetitiveReport


def suggested_questions(company: str, report: CompetitiveReport, competitors: list[dict]) -> list[str]:
    names = [c["name"] for c in competitors if c.get("name")][:3]
    out: list[str] = []

    if names:
        out.append(f"How does {company} compare to {names[0]} on pricing?")
        out.append(f"What are {company}'s biggest weaknesses versus {', '.join(names)}?")
    else:
        out.append(f"Who are {company}'s biggest competitors right now?")

    if report.swot.threats:
        out.append(f"How serious is the threat: {report.swot.threats[0]}?")
    if report.opportunities:
        out.append(f"Tell me more about: {report.opportunities[0].text}")
    if report.recommendations:
        out.append(f"Why do you recommend: {report.recommendations[0].action}?")
    if names:
        out.append(f"What has {names[0]} launched or changed recently?")

    return out[:6]
