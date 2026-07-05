"""Chat over the agents' own intel, falling back to a live web search.

Flow per question:
  1. Look up the company's stored intel — the latest completed CompetitiveReport
     (or a pinned run_id), its confirmed competitors, and the raw per-agent
     signals that fed the report (repository.get_signals_for_run — richer than
     the report alone since the report is a synthesis, signals are the source
     items with their own evidence_ids).
  2. Ask the LLM to answer STRICTLY from that context. The schema carries
     needs_live_data so the model itself flags "not covered here" instead of a
     separate classifier call.
  3. If there's no stored report at all, or the model set needs_live_data, run
     a live search (search_chain — same Tavily->Serper->scrape chain the
     agents use) and ask again with those results as context.

Runs in a background task (mirrors lifecycle.py's pattern) so the endpoint
returns immediately and the frontend polls GET /chat/{chat_id} for progress,
same shape as the existing run poller.
"""
import logging

from ..db import repository
from ..models import ChatAnswer
from . import chat_store
from . import llm_router
from . import search_chain as search_mod

logger = logging.getLogger(__name__)

_MAX_CONTEXT_CHARS = 8000

# Questions asking about "now"/"latest" describe facts that go stale the moment
# the stored report was written. The model is asked to set needs_live_data
# itself, but a reasoning-lane model under a tight token budget sometimes
# answers confidently from stale stored context instead of flagging it — so
# back that self-classification with a keyword heuristic that forces the live
# search step regardless of what the model decided.
_FRESHNESS_KEYWORDS = (
    "latest", "current", "currently", "now", "today", "this week", "this month",
    "recent", "recently", "up to date", "up-to-date", "as of today", "just announced",
    "stock price", "share price", "valuation",
)


def _needs_freshness(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in _FRESHNESS_KEYWORDS)


def start_chat(chat_id: str, company: str, question: str, run_id: str | None) -> None:
    """Background entry point — never raises past itself (mirrors lifecycle's
    guarded background tasks); failures land as a typed chat_store.fail()."""
    try:
        _run(chat_id, company, question, run_id)
    except Exception:  # noqa: BLE001
        logger.exception("chat %s failed", chat_id)
        chat_store.fail(chat_id, "internal error answering the question")


def _emit(chat_id: str):
    return lambda agent, msg: chat_store.emit(chat_id, agent, msg)


def _stored_context(company: str, run_id: str | None) -> tuple[str, list[str], str | None]:
    """Returns (context_text, known_evidence_ids, resolved_run_id). context_text
    is "" when nothing is on file for this company/run."""
    resolved = run_id
    if not resolved:
        found = repository.find_completed_report(company)
        resolved = found["run_id"] if found else None
    if not resolved:
        return "", [], None

    row = repository.get_report(resolved)
    report = (row or {}).get("report") or {}
    competitors = repository.get_competitors(resolved)
    # get_signals_for_run is DB-only (no in-memory fallback, per data_dictionary.md —
    # signals never had one) — swallow so MOCK_MODE / no-DATABASE_URL local runs still
    # answer from the report + competitors instead of failing the whole chat.
    try:
        signals = repository.get_signals_for_run(resolved)
    except RuntimeError:
        signals = []

    if not report and not competitors and not signals:
        return "", [], resolved

    parts = [f"COMPANY: {company}", f"ANALYSIS DATE: {report.get('analysis_date', 'unknown')}",
             f"THREAT LEVEL: {report.get('threat_level', 'unknown')}",
             f"EXECUTIVE SUMMARY: {report.get('executive_summary', '')}"]
    if competitors:
        parts.append("COMPETITORS: " + ", ".join(f"{c['name']} ({c.get('category', 'direct')})" for c in competitors))
    swot = report.get("swot") or {}
    for k in ("strengths", "weaknesses", "opportunities", "threats"):
        if swot.get(k):
            parts.append(f"SWOT {k.upper()}: " + "; ".join(swot[k]))
    if report.get("head_to_head"):
        for row_ in report["head_to_head"]:
            rivals = ", ".join(f"{name}: {cell.get('value', '')}" for name, cell in (row_.get("rivals") or {}).items())
            parts.append(f"HEAD-TO-HEAD {row_.get('metric', '')}: you={row_.get('you', '')} · {rivals}")
    evidence_ids: list[str] = []
    for opp in report.get("opportunities") or []:
        parts.append(f"OPPORTUNITY [{opp.get('claim_ref', '')}]: {opp.get('text', '')}")
        evidence_ids.extend(opp.get("evidence_ids") or [])
    for rec in report.get("recommendations") or []:
        parts.append(f"RECOMMENDATION [{rec.get('claim_ref', '')}]: {rec.get('action', '')} — {rec.get('rationale', '')}")
        evidence_ids.extend(rec.get("evidence_ids") or [])
    for sig in signals:
        payload = sig.get("payload") or {}
        parts.append(f"SIGNAL ({sig.get('agent')}/{sig.get('competitor')}/{sig.get('type')}): {payload}")
        evidence_ids.extend(sig.get("evidence_ids") or [])

    text = "\n".join(parts)[:_MAX_CONTEXT_CHARS]
    return text, list(dict.fromkeys(evidence_ids)), resolved


def _live_context(company: str, question: str, emit) -> str:
    queries = [f"{company} {question}", question]
    results = search_mod.search_all(queries, emit)
    if not results:
        return ""
    lines = [f"SOURCE: {r.get('url', '')}\n{r.get('title', '')} — {r.get('content', '')}" for r in results[:8]]
    return "\n\n".join(lines)[:_MAX_CONTEXT_CHARS]


_ANTI_HALLUCINATION_RULES = (
    "Rules: "
    "1) Use ONLY facts stated in CONTEXT below — never invent numbers, dates, "
    "competitor names, or claims that aren't there, even if they sound plausible. "
    "2) If CONTEXT only partially covers the question, answer the part it covers "
    "and explicitly say what it doesn't cover, rather than filling the gap with a guess. "
    "3) If CONTEXT has nothing relevant, set needs_live_data=true and leave answer "
    'empty rather than answering from general knowledge. '
    "4) Never state a fact more confidently than the source does — hedge ('as of the "
    "last analysis...', 'reported to be...') rather than asserting stale data as current fact."
)


def _ask(system_note: str, context: str, question: str, emit) -> ChatAnswer:
    prompt = (
        f"{system_note}\n\n{_ANTI_HALLUCINATION_RULES}\n\n"
        f"CONTEXT:\n{context or '(no context available)'}\n\n"
        f"QUESTION: {question}\n\n"
        'Reply as JSON: {"answer": str, "needs_live_data": bool, "evidence_ids": [str]}. '
        "needs_live_data=true ONLY if the context above does not contain enough to answer. "
        "evidence_ids: copy any [claim_ref]-style ids the answer relies on from the context, else []."
    )
    try:
        result, _lane = llm_router.complete("reason", prompt, ChatAnswer, emit)
        return result
    except RuntimeError:
        return ChatAnswer(answer="", needs_live_data=True, evidence_ids=[])


_NO_ANSWER_STORED = (
    "I don't have anything on file for this, and the live search didn't surface a "
    "reliable source either. This may mean it hasn't come up in an analysis run yet — "
    "try rephrasing, narrowing the question, or running a fresh analysis for this company."
)
_NO_ANSWER_LIVE_ONLY = (
    "There's no stored analysis for this company yet, and a live web search didn't turn "
    "up anything usable for this question. Try rephrasing, or run an analysis for this "
    "company first so there's stored intel to draw on."
)
_STALE_NOTE = ("Note: this is based on the last completed analysis (dated {date}), not a "
               "live check — the underlying facts may have changed since then.")


def _staleness_note(context: str) -> str:
    for line in context.splitlines():
        if line.startswith("ANALYSIS DATE: "):
            date = line.removeprefix("ANALYSIS DATE: ").strip()
            if date and date != "unknown":
                return _STALE_NOTE.format(date=date)
    return ""


def _run(chat_id: str, company: str, question: str, run_id: str | None) -> None:
    emit = _emit(chat_id)
    emit("chat", f"checking stored intel for {company}")
    context, evidence_ids, resolved_run_id = _stored_context(company, run_id)

    if not context:
        emit("chat", "no stored analysis on file · searching live")
        live = _live_context(company, question, emit)
        answer = _ask(f"You are Rivalyze's assistant. Answer the question about {company} "
                       "using ONLY the live web search results below.", live, question, emit)
        chat_store.finish(chat_id, answer.answer.strip() or _NO_ANSWER_LIVE_ONLY, "live", [])
        emit("system", "done")
        return

    force_live = _needs_freshness(question)
    if force_live:
        emit("chat", "question asks for current/latest info · searching live")
        answer = ChatAnswer(answer="", needs_live_data=True, evidence_ids=[])
    else:
        emit("chat", "found stored analysis · answering from it")
        answer = _ask(f"You are Rivalyze's assistant. Answer the question about {company} "
                      "using ONLY the analysis context below (a prior agent run's report, "
                      "competitors, and signals).", context, question, emit)

    if not force_live and not answer.needs_live_data and answer.answer.strip():
        cited = [e for e in answer.evidence_ids if e in evidence_ids] or evidence_ids
        note = _staleness_note(context)
        final = answer.answer.strip() + (f"\n\n{note}" if note else "")
        chat_store.finish(chat_id, final, "stored", cited)
        emit("system", "done")
        return

    emit("chat", "stored analysis doesn't cover this · searching live")
    live = _live_context(company, question, emit)
    live_answer = _ask(f"You are Rivalyze's assistant. The stored analysis below didn't fully "
                       f"answer the question about {company}; supplement it with the live web "
                       "results that follow, and answer using both.",
                       context + "\n\nLIVE SEARCH RESULTS:\n" + (live or "(none found)"),
                       question, emit)
    final_answer = live_answer.answer.strip() or answer.answer.strip() or _NO_ANSWER_STORED
    chat_store.finish(chat_id, final_answer, "mixed" if live else "stored", evidence_ids)
    emit("system", "done")
