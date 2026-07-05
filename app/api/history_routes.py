"""History + report export — GET /api/v1/history, GET /api/v1/reports/{run_id}/export.

Owner: Dharvi. Registered alongside app/api/routes.py in main.py (Drashti owns
the includes/auth dependency/CORS; this router reuses her require_token).
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ..core.auth import get_current_user, require_token
from ..core.delta import compute_delta
from ..core.export import report_to_markdown
from ..db import connection, repository
from ..models import HistoryEntry, UserPublic

router = APIRouter(prefix="/api/v1")


def _flag_new_changes(rows: list[dict]) -> list[dict]:
    """Monitor Delta badge: set has_new=True on each company's NEWEST row when
    its latest run carries signals the previous run didn't (computed at read
    time — nothing extra is stored). Rows are newest-first, so the first row
    seen per company is the one that gets the flag; older rows stay False.
    Signals are DB-only, so this is skipped entirely without a database."""
    seen: set[str] = set()
    for row in rows:
        row["has_new"] = False
        cid = row.get("company_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        runs = repository.get_latest_completed_runs(cid, limit=2)
        # flag only the row that IS the latest run, and only when a previous
        # run exists to diff against (first_run companies stay False)
        if len(runs) == 2 and runs[0]["id"] == row.get("run_id"):
            new = compute_delta(repository.get_signals_for_run(runs[1]["id"]),
                                repository.get_signals_for_run(runs[0]["id"]))
            row["has_new"] = bool(new)
    return rows


@router.get("/history", response_model=list[HistoryEntry])
def history(
    company: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: UserPublic = Depends(get_current_user),
) -> list[dict]:
    """The CALLER's completed runs, newest first; ?company= does an ILIKE substring
    match. Scoped to current_user (the run owner stamped at /analyze time) so one
    user never sees another's runs. Each company's newest row carries has_new — the
    "new changes" popup flag (see HistoryEntry.has_new; details via GET
    /companies/{slug}/delta)."""
    rows = repository.get_history(limit=limit, company=company, user_id=current_user.user_id)
    if connection.is_enabled():
        rows = _flag_new_changes(rows)
    return rows


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
