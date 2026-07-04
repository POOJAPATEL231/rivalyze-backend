"""Warm-up script — sequentially analyze every seed company against the live
API so demo day starts with warm caches and real /history entries instead of
cold first-runs.

Owner: Dharvi. Spec: dharvi_task.md Module 4 / mihir_prompt.txt (below his
"=== SPECS BELOW THIS LINE ARE DHARVI'S ===" marker). For each company in
seed/warmup_companies.json, SEQUENTIALLY (never parallel — free-tier rate
limits): POST /api/v1/analyze, poll /api/v1/runs/{job_id} every 5s up to 6
minutes; on failure, retry once after a 60s cooldown, then record failed and
continue. Writes seed/warmup_manifest.json (outcome + duration + lane_stats
per company). --budget N stops the run before the next company once the
tavily credit counter (GET /api/v1/health's `counters` field) reaches N.

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


def _analyze(client: httpx.Client, company: str) -> str:
    """POST /analyze for one company; returns its job_id."""
    r = client.post("/api/v1/analyze", json={"company": company, "domain": ""}, headers=_headers())
    r.raise_for_status()
    return r.json()["job_id"]


def _poll(client: httpx.Client, job_id: str) -> dict:
    """Poll /runs/{job_id} every 5s up to 6 minutes.

    Returns the final RunStatus dict once status is completed/failed, or a
    synthetic failed status if the deadline passes first.
    """
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        r = client.get(f"/api/v1/runs/{job_id}", headers=_headers())
        r.raise_for_status()
        status = r.json()
        if status["status"] in ("completed", "failed"):
            return status
        time.sleep(POLL_INTERVAL_S)
    return {"status": "failed", "error": "poll timeout after 6 minutes", "lane_stats": {}}


def _run_one(client: httpx.Client, company: str) -> dict:
    """Analyze one company end-to-end; retry exactly once after a 60s
    cooldown on failure, then give up and record it as failed."""
    result: dict = {}
    duration = 0.0
    for attempt in (1, 2):
        t0 = time.monotonic()
        try:
            job_id = _analyze(client, company)
            result = _poll(client, job_id)
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
    """True once the tavily credit counter reaches `budget`. A health-check
    hiccup must never abort the whole warm-up run, so any HTTP error here
    is treated as "not exceeded" rather than raised."""
    if budget is None:
        return False
    try:
        r = client.get("/api/v1/health")
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
