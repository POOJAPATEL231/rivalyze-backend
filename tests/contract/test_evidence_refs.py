"""The by-ids evidence route (drawer for recommendations/opportunities, which cite
evidence_ids rather than a claim_ref) must be registered alongside the claim_ref
route, and the literal path must not be shadowed by /evidence/{claim_ref}."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_both_evidence_routes_registered():
    paths = app.openapi()["paths"]
    assert "/api/v1/evidence-refs" in paths            # by evidence_ids
    assert "/api/v1/evidence/{claim_ref}" in paths     # by claim_ref


def test_evidence_refs_not_shadowed_by_claim_ref_route():
    # Hitting /evidence-refs must route to the by-ids handler, which requires the
    # `ids` query param -> 422 when it is missing. If the path-param route had
    # shadowed it (claim_ref="refs"), the missing param would be run_id and the
    # error detail would differ. Either way it must NOT 404 on the path itself.
    r = client.get("/api/v1/evidence-refs")
    assert r.status_code != 404
