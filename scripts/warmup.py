"""Warm-up runner — pre-populate caches + reports for an instant demo.

Owner: Dharvi. Drives the FULL two-phase flow for each seed company so the
demo's persistence-first path (`/analyze` -> instant completed) is hot:
  POST /analyze -> poll to awaiting_confirmation -> POST /confirm (auto-approve
  the discovered rivals) -> poll to completed.

Sequential on purpose (kind to rate limits and provider budgets); retries a
company once after a pause. Writes a small manifest of outcomes.

Usage:
  python -m scripts.warmup --base http://localhost:8000 --token $BEARER_TOKEN
  python -m scripts.warmup --seed seed/warmup_companies.json --out warmup_manifest.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

_POLL_TIMEOUT_S = 180
_POLL_EVERY_S = 2.0


def _load_seed(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    # accept either ["Notion", ...] or [{"company":"Notion","domain":"..."}, ...]
    out = []
    for item in data:
        if isinstance(item, str):
            out.append({"company": item, "domain": ""})
        elif isinstance(item, dict) and item.get("company"):
            out.append({"company": item["company"], "domain": item.get("domain", "")})
    return out


def _poll_until(client: httpx.Client, job_id: str, wanted: set[str]) -> dict:
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get(f"/api/v1/runs/{job_id}").json()
        if last.get("status") in wanted:
            return last
        time.sleep(_POLL_EVERY_S)
    return last


def _run_one(client: httpx.Client, company: str, domain: str) -> dict:
    r = client.post("/api/v1/analyze", json={"company": company, "domain": domain})
    r.raise_for_status()
    body = r.json()
    if body.get("status") == "completed":
        return {"company": company, "status": "completed", "cached": True}

    job_id = body["job_id"]
    gate = _poll_until(client, job_id, {"awaiting_confirmation", "completed", "failed"})
    if gate.get("status") == "completed":
        return {"company": company, "status": "completed", "cached": True}
    if gate.get("status") != "awaiting_confirmation":
        return {"company": company, "status": gate.get("status", "unknown"), "job_id": job_id}

    competitors = (gate.get("result") or {}).get("competitors") or []
    if not competitors:
        return {"company": company, "status": "no_competitors", "job_id": job_id}

    c = client.post(f"/api/v1/runs/{job_id}/confirm",
                    json={"confirmed_competitors": competitors})
    c.raise_for_status()
    final = _poll_until(client, job_id, {"completed", "failed"})
    return {"company": company, "status": final.get("status", "timeout"),
            "job_id": job_id, "run_id": final.get("run_id")}


def main() -> int:
    ap = argparse.ArgumentParser(description="Rivalyze two-phase warm-up runner")
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--token", default="")
    ap.add_argument("--seed", default="seed/warmup_companies.json")
    ap.add_argument("--out", default="warmup_manifest.json")
    args = ap.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    seed = _load_seed(args.seed)
    manifest: list[dict] = []

    with httpx.Client(base_url=args.base, headers=headers, timeout=60) as client:
        for entry in seed:
            company, domain = entry["company"], entry["domain"]
            try:
                result = _run_one(client, company, domain)
            except Exception as exc:  # noqa: BLE001 — retry once, then record failure
                print(f"[warmup] {company}: {type(exc).__name__} — retrying in 60s", file=sys.stderr)
                time.sleep(60)
                try:
                    result = _run_one(client, company, domain)
                except Exception as exc2:  # noqa: BLE001
                    result = {"company": company, "status": "error", "error": str(exc2)}
            print(f"[warmup] {company}: {result['status']}")
            manifest.append(result)

    Path(args.out).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    completed = sum(1 for m in manifest if m["status"] == "completed")
    print(f"[warmup] {completed}/{len(manifest)} completed · manifest -> {args.out}")
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
