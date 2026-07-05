"""Monitor Delta v0 — GET /api/v1/companies/{slug}/delta. Owner: Drashti.

"What's new since last run": diffs the signals of the two most-recent COMPLETED
runs of a company (dedupe logic in app/core/delta.py). Pure DB read — NO AI
call, NO search, NO credits. Registered alongside routes.py in main.py; reuses
the shared require_token dependency.

.NET reader mapping: a thin GET action over a "diff two result sets" query —
like a stored proc comparing this week's rows to last week's.
"""
from fastapi import APIRouter, Depends, HTTPException

from ..core.auth import require_token
from ..core.delta import compute_delta
from ..db import repository
from ..models import DeltaResponse

router = APIRouter(prefix="/api/v1")


@router.get("/companies/{slug}/delta", response_model=DeltaResponse,
            response_model_exclude_none=True, dependencies=[Depends(require_token)])
def company_delta(slug: str) -> DeltaResponse:
    """Two 200 shapes (exclude_none drops the unused optionals):
      - previous run exists: {company, since, count, new_signals}
      - zero or one completed run: {count: 0, new_signals: [], first_run: true}
    404 only for an unknown slug."""
    company = repository.get_company_by_slug(slug)
    if company is None:
        raise HTTPException(status_code=404, detail="company not found")

    runs = repository.get_latest_completed_runs(company["id"], limit=2)
    if len(runs) < 2:
        return DeltaResponse(count=0, new_signals=[], first_run=True)

    r1, r0 = runs[0], runs[1]  # latest, previous
    new = compute_delta(repository.get_signals_for_run(r0["id"]),
                        repository.get_signals_for_run(r1["id"]))
    return DeltaResponse(company=company["name"], since=r0["finished_at"],
                        count=len(new), new_signals=new)
