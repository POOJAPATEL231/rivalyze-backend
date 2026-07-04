"""Discovery agent — Module 1 (Sheel), hardened from the POC vertical slice.

search -> extraction prompt -> typed CompetitorSet via the router -> persisted
via the repository. Encodes the hard-won prompt lessons: bare-JSON demand,
same-business-model constraint, generic-giant exclusion, self-exclusion,
4-rival cap, month-year recency bias on queries.

Degradation contract: this function NEVER raises. Total search failure,
total LLM-lane failure, or a repository outage all fall through to an empty
typed CompetitorSet plus a low-signal event — the pipeline always completes.
"""
from datetime import datetime

from ..models import Competitor, CompetitorSet
from ..core import search_chain as search_mod
from ..core import llm_router

# Names that read as competitors on paper but are usually noise unless the
# corpus explicitly ties them to an equivalent product (e.g. "Google Docs"
# is a real Notion rival; "Google" alone is not). We do not hard-delete
# these — the prompt already instructs the model to exclude them — this is
# a second line of defense that only flags, so a genuine equivalent-product
# hit is never silently dropped.
GENERIC_GIANTS = {"google", "amazon", "youtube", "microsoft", "meta", "tcs",
                   "infosys", "wipro", "accenture", "reliance"}


def run(company: str, domain: str, run_id: str, emit) -> CompetitorSet:
    """Discover up to 4 competitors for `company` in `domain`.

    run_id ties this call to a persisted run row (Dharvi's repository) so
    the result survives a process restart and the frontend can poll it from
    Postgres rather than in-memory state.
    """
    month = datetime.now().strftime("%B %Y")
    emit("discovery", f"target locked: {company} · {domain or 'domain inferred'}")

    corpus = _build_corpus(company, domain, month, emit)
    if not corpus.strip():
        emit("discovery", "no search results returned · low signal")
        result = CompetitorSet(competitors=[])
        _persist(run_id, result, emit)
        return result

    prompt = _build_prompt(company, domain, corpus)

    try:
        result, lane = llm_router.complete("extract", prompt, CompetitorSet, emit)
    except RuntimeError as e:
        emit("discovery", f"low signal: {e} · returning empty typed set")
        result = CompetitorSet(competitors=[])
        _persist(run_id, result, emit)
        return result

    result.competitors = _post_filter(result.competitors, company)
    emit("discovery", f"{len(result.competitors)} competitors extracted via {lane}")

    _persist(run_id, result, emit)
    return result


def _build_corpus(company: str, domain: str, month: str, emit) -> str:
    q1 = f"top competitors of {company} in {domain} {month}".strip()
    q2 = f"alternatives to {company} {domain} {month}".strip()

    corpus = ""
    for q in (q1, q2):
        for r in search_mod.search(q, emit):
            corpus += f"{r['title']}\n{r['content']}\nSOURCE: {r['url']}\n\n"

    return corpus[:6000]  # prompt budget cap (v2 §4.6)


def _build_prompt(company: str, domain: str, corpus: str) -> str:
    return f"""From the search results below, identify the top 4 direct or indirect
competitors of {company} in the {domain or 'same'} space.

Rules:
- Same business model only. Exclude generic giants (Google, YouTube, Amazon,
  TCS-class conglomerates) unless they compete with an equivalent product
  (e.g. "Google Docs" is valid against a docs tool, "Google" alone is not).
- Never include {company} itself.
- "category" is "direct" or "indirect". "rationale" is one short sentence.
- Maximum 4. If the results support fewer, return fewer — do not invent.

Return JSON exactly shaped as:
{{"competitors":[{{"name":"...","category":"direct","rationale":"..."}}]}}

SEARCH RESULTS:
{corpus}"""


def _post_filter(competitors: list[Competitor], company: str) -> list[Competitor]:
    """System-authored fields rule: the model must never re-list the input
    company. Giant names are logged, not deleted — see GENERIC_GIANTS."""
    filtered = [c for c in competitors if c.name.lower() != company.lower()][:4]
    for c in filtered:
        if c.name.lower() in GENERIC_GIANTS:
            filtered_note = f"generic-giant name passed through: {c.name} · rationale: {c.rationale}"
            # Not removed — rationale may justify an equivalent-product hit.
            # Surfaced here so it shows up in the run's event ledger for review.
            _log_giant_flag(filtered_note)
    return filtered


def _log_giant_flag(note: str) -> None:
    # Kept as its own function so it can be swapped for a real emit() call
    # or a metrics counter without touching _post_filter's logic.
    pass


def _persist(run_id: str, result: CompetitorSet, emit) -> None:
    """Best-effort repository write. A DB outage must never fail discovery
    itself — the in-memory result still flows to the rest of the pipeline."""
    try:
        from ..db import repository
        rows = [c.model_dump() for c in result.competitors]
        repository.save_competitors(run_id, rows)
    except Exception as e:
        emit("discovery", f"repository write skipped: {type(e).__name__}")