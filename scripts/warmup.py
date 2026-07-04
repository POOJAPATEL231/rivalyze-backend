"""Warm-up script — drives the full two-phase pipeline for every seed company
so demo day starts with warm caches, real /history entries, and completed
reports instead of cold first-runs.

Owner: Dharvi. Spec: dharvi_task.md Module 4 / mihir_prompt.txt (below his
"=== SPECS BELOW THIS LINE ARE DHARVI'S ===" marker), updated for the
two-phase pipeline (Rivalyze_TwoPhase_Pipeline.md) that landed after the
original spec was written: POST /analyze now only runs discovery and PARKS
the run at awaiting_confirmation; reaching completed requires auto-approving
the proposed rivals via POST /runs/{id}/confirm and polling again. For each
company in seed/warmup_companies.json, SEQUENTIALLY (never parallel —
free-tier rate limits):
  POST /analyze -> poll to {awaiting_confirmation, completed, failed}
  -> [if awaiting_confirmation] POST /confirm (approve exactly as discovered)
  -> poll to {completed, failed}
On any failure (network error OR a pipeline outcome other than completed),
retry the WHOLE company once after a 60s cooldown, then record failed and
continue. Writes seed/warmup_manifest.json (outcome + duration + lane_stats
per company). --budget N stops the run before the next company once the
tavily credit counter (GET /api/v1/credits, authed) reaches N.

Run (against an already-running server):
  python -m scripts.warmup
  python -m scripts.warmup --budget 800
  python -m scripts.warmup --base-url http://localhost:8000 --seed seed/warmup_companies.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console

# Some Windows shells attach a stdout stream whose encoding is the legacy
# console code page (cp1252), which can't represent the checkmark/cross
# glyphs below -> UnicodeEncodeError. Reconfiguring to utf-8 (errors=
# "replace" as a last-resort fallback) fixes this at the source, on any
# platform where the stream supports reconfigure() (Python 3.7+).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

console = Console()

POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 6 * 60
RETRY_COOLDOWN_S = 60

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED = REPO_ROOT / "seed" / "warmup_companies.json"
DEFAULT_MANIFEST = REPO_ROOT / "seed" / "warmup_manifest.json"


def _headers() -> dict:
    token = os.getenv("BEARER_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _analyze(client: httpx.Client, company: str) -> dict:
    """POST /analyze for one company; returns the full response body
    ({job_id, status}) — status is "completed" on a persistence-first hit,
    "running_discovery" otherwise."""
    r = client.post("/api/v1/analyze", json={"company": company, "domain": ""}, headers=_headers())
    r.raise_for_status()
    return r.json()


def _poll(client: httpx.Client, job_id: str, wanted: set[str]) -> dict:
    """Poll /runs/{job_id} every 5s up to 6 minutes until status is in `wanted`.

    Returns the final RunStatus dict, or a synthetic failed status if the
    deadline passes first.
    """
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        r = client.get(f"/api/v1/runs/{job_id}", headers=_headers())
        r.raise_for_status()
        status = r.json()
        if status["status"] in wanted:
            return status
        time.sleep(POLL_INTERVAL_S)
    return {"status": "failed", "error": "poll timeout after 6 minutes", "lane_stats": {}}


def _confirm(client: httpx.Client, job_id: str, competitors: list[dict]) -> None:
    """POST /confirm, auto-approving EXACTLY the proposed rivals (no edits) —
    a warm-up run just needs a completed report, not a curated one."""
    r = client.post(
        f"/api/v1/runs/{job_id}/confirm",
        json={"confirmed_competitors": competitors},
        headers=_headers(),
    )
    r.raise_for_status()


def _drive_two_phase(client: httpx.Client, company: str) -> dict:
    """One pass through the full two-phase flow for `company` (no retry —
    _run_one wraps this with the retry-once policy). Returns the final
    RunStatus dict (status completed/failed) with job_id always set."""
    body = _analyze(client, company)
    job_id = body["job_id"]
    status = _poll(client, job_id, {"awaiting_confirmation", "completed", "failed"})

    if status["status"] == "awaiting_confirmation":
        competitors = (status.get("result") or {}).get("competitors") or []
        if not competitors:
            return {"job_id": job_id, "status": "failed", "error": "no competitors proposed", "lane_stats": {}}
        _confirm(client, job_id, competitors)
        status = _poll(client, job_id, {"completed", "failed"})

    status.setdefault("job_id", job_id)
    return status


def _run_one(client: httpx.Client, company: str) -> dict:
    """Drive one company through the two-phase flow end-to-end; retry the
    WHOLE thing once after a 60s cooldown on any failure (network error or a
    pipeline outcome other than completed), then give up and record failed."""
    result: dict = {}
    duration = 0.0
    for attempt in (1, 2):
        t0 = time.monotonic()
        try:
            result = _drive_two_phase(client, company)
        except httpx.HTTPError as e:
            result = {"status": "failed", "error": str(e), "lane_stats": {}}
        duration = round(time.monotonic() - t0, 1)

        if result.get("status") == "completed":
            return {
                "company": company,
                "outcome": "completed",
                "attempt": attempt,
                "duration_s": duration,
                "lane_stats": result.get("lane_stats", {}),
                "job_id": result.get("job_id"),
                "run_id": result.get("run_id"),
            }
        if attempt == 1:
            console.print(f"  [yellow]retrying {company} in {RETRY_COOLDOWN_S}s...[/yellow]")
            time.sleep(RETRY_COOLDOWN_S)

    return {
        "company": company,
        "outcome": "failed",
        "attempt": 2,
        "duration_s": duration,
        "lane_stats": result.get("lane_stats", {}),
        "error": result.get("error"),
    }


def _budget_exceeded(client: httpx.Client, budget: Optional[int]) -> bool:
    """True once the tavily credit counter reaches `budget`. A credits-check
    hiccup must never abort the whole warm-up run, so any HTTP error here
    is treated as "not exceeded" rather than raised.

    Reads GET /api/v1/credits (authed), not /health: /health stays open and
    minimal on purpose — provider usage is gated behind require_token like
    the rest of the contract, so this call needs the same auth header.
    """
    if budget is None:
        return False
    try:
        r = client.get("/api/v1/credits", headers=_headers())
        r.raise_for_status()
        tavily = r.json().get("counters", {}).get("tavily", 0)
        return tavily >= budget
    except httpx.HTTPError:
        return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Warm up the Rivalyze API by pre-analyzing seed companies.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--budget", type=int, default=None,
                        help="stop before the next company once the tavily credit counter reaches this")
    args = parser.parse_args(argv)

    companies: list[str] = json.loads(args.seed.read_text(encoding="utf-8"))
    manifest: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "entries": []}

    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        for company in companies:
            if _budget_exceeded(client, args.budget):
                console.print(f"[red]budget of {args.budget} reached — stopping before {company}[/red]")
                manifest["stopped_early"] = True
                break

            entry = _run_one(client, company)
            manifest["entries"].append(entry)

            mark = "[green]✓[/green]" if entry["outcome"] == "completed" else "[red]✗[/red]"
            cache_hits = entry["lane_stats"].get("cache_hits", 0)
            console.print(f"{mark} {company:<20} {entry['duration_s']:>6.1f}s  cache_hits={cache_hits}")

    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    failed = [e for e in manifest["entries"] if e["outcome"] == "failed"]
    total = len(manifest["entries"])
    console.print(f"\n{total - len(failed)}/{total} completed — manifest written to {args.manifest}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
