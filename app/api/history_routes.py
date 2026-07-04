"""History + report export — GET /api/v1/history, GET /api/v1/reports/{run_id}/export.

Owner: Dharvi. Registered alongside app/api/routes.py in main.py (Drashti owns
the includes/auth dependency/CORS; this router reuses her require_token).
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ..core.auth import require_token
from ..core.export import report_to_markdown
from ..db import repository
from ..models import HistoryEntry

router = APIRouter(prefix="/api/v1")


@router.get("/history", response_model=list[HistoryEntry], dependencies=[Depends(require_token)])
def history(
    company: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    """Completed runs, newest first; ?company= does an ILIKE substring match."""
    return repository.get_history(limit=limit, company=company)


@router.get("/reports/{run_id}/export", dependencies=[Depends(require_token)])
def export_report(run_id: str, format: str = Query(default="md")) -> Response:
    """text/markdown attachment for a completed run's report.

    The rendered markdown is cached on reports.md_export on first export
    (schema.sql documents it as exactly that) so a repeat download for the
    same run is a plain read, not a re-render.

    Untrusted-content note: this markdown embeds model-authored text
    (executive_summary, SWOT items, rival names, opportunity/recommendation
    text) verbatim. Served here as an `attachment` (download, not inline
    render) so the endpoint itself is safe either way — but any consumer that
    renders this markdown AS HTML must disable/sanitize raw HTML, or a
    model-injected `<script>`/`<img onerror>` or a `[x](javascript:...)` link
    becomes stored XSS.
    """
    if format != "md":
        raise HTTPException(status_code=400, detail="unsupported format (only 'md' is available)")

    row = repository.get_report(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")

    md = row.get("md_export")
    if not md:
        report = row["report"]
        md = report_to_markdown(report, repository.get_evidence_by_ids)
        repository.save_report(run_id, report, md)

    company_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", row["report"].get("company", "report")).strip("-") or "report"
    filename = f"{company_slug}-{run_id}.md"
    return Response(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
