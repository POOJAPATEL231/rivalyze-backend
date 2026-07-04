"""Discovery agent — the thin vertical slice's real agent.
search -> extraction prompt -> typed CompetitorSet via the router.

Encodes the hard-won prompt lessons: bare-JSON demand, same-business-model
constraint, generic-giant exclusion, self-exclusion, 4-rival cap, month-year
recency bias on queries. Graceful degradation: on total failure it returns
an EMPTY typed set + an event — never a raw error.

Owner: Sheel (hardens this first, e2e, Sat 13:00). Base ported from the POC.
"""
from datetime import datetime

from ..core import llm_router
from ..core import search_chain as search_mod
from ..models import CompetitorSet


def run(company: str, domain: str, emit) -> tuple[CompetitorSet, str]:
    month = datetime.now().strftime("%B %Y")
    emit("discovery", f"target locked: {company} · {domain or 'domain inferred'}")

    q1 = f"top competitors of {company} in {domain} {month}".strip()
    q2 = f"alternatives to {company} {domain} {month}".strip()
    corpus = ""
    for q in (q1, q2):
        for r in search_mod.search(q, emit):
            corpus += f"{r['title']}\n{r['content']}\nSOURCE: {r['url']}\n\n"
    corpus = corpus[:6000]  # prompt budget cap (v2 §4.6)

    prompt = f"""From the search results below, identify the top 4 direct or indirect
competitors of {company} in the {domain or 'same'} space.

Rules:
- Same business model only. Exclude generic giants (Google, YouTube, Amazon,
  TCS-class conglomerates) unless they compete with an equivalent product.
- Never include {company} itself.
- "category" is "direct" or "indirect". "rationale" is one short sentence.
- Maximum 4. If the results support fewer, return fewer — do not invent.

Return JSON exactly shaped as:
{{"competitors":[{{"name":"...","category":"direct","rationale":"..."}}]}}

SEARCH RESULTS:
{corpus}"""

    try:
        result, lane = llm_router.complete("extract", prompt, CompetitorSet, emit)
        # defensive post-filter (system-authored fields rule)
        result.competitors = [c for c in result.competitors
                              if c.name.lower() != company.lower()][:4]
        emit("discovery",
             f"{len(result.competitors)} competitors extracted via {lane}")
        return result, lane
    except RuntimeError as e:
        emit("discovery", f"low signal: {e} · returning empty typed set")
        return CompetitorSet(competitors=[]), "none"
