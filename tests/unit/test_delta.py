"""Unit tests for app/core/delta.py — pure logic, no DB, no app import.

The normalization rules are the make-or-break of the feature (dedupe, or the
badge is noise): the same news re-found with different punctuation/casing must
NOT count as new.

Run:  pytest tests/unit/test_delta.py -q
"""
from app.core.delta import compute_delta, identity_key, normalize_headline


def _sig(agent="news", competitor="Acme", type="funding", payload=None, evidence_ids=None):
    return {"agent": agent, "competitor": competitor, "type": type,
            "payload": payload or {}, "evidence_ids": evidence_ids or []}


# ============================ normalize_headline ============================
def test_normalize_lowercases_and_strips_punctuation():
    assert normalize_headline({"event": "Acme Raises $50M, Series-B!!"}) == "acme raises 50m seriesb"


def test_normalize_trims_and_truncates_to_80():
    long = "  " + "a" * 100 + "  "
    out = normalize_headline({"event": long})
    assert out == "a" * 80


def test_normalize_prefers_event_over_headline():
    assert normalize_headline({"event": "From Event", "headline": "From Headline"}) == "from event"


def test_normalize_falls_back_to_headline():
    assert normalize_headline({"headline": "Only Headline"}) == "only headline"


def test_normalize_missing_both_keys_is_empty():
    assert normalize_headline({}) == ""
    assert normalize_headline({"event": None, "headline": None}) == ""


# =============================== identity_key ===============================
def test_identity_key_shape():
    k = identity_key(_sig(payload={"event": "Launch: V2!"}))
    assert k == ("news", "Acme", "funding", "launch v2")


# =============================== compute_delta ==============================
def test_identical_runs_produce_empty_delta():
    sigs = [_sig(payload={"event": "Acme raises $50M"}),
            _sig(agent="product", type="launch", payload={"event": "V2 shipped"})]
    assert compute_delta(sigs, list(sigs)) == []


def test_reworded_punctuation_and_case_still_match():
    prev = [_sig(payload={"event": "Acme raises $50M Series B"})]
    curr = [_sig(payload={"event": "ACME Raises $50m, Series B!"})]
    assert compute_delta(prev, curr) == []


def test_genuinely_new_signal_is_returned_shaped():
    prev = [_sig(payload={"event": "Old news"})]
    curr = [_sig(payload={"event": "Old news"}),
            _sig(type="pricing", payload={"event": "New pricing page"}, evidence_ids=["ev-1"])]
    out = compute_delta(prev, curr)
    assert out == [{
        "agent": "news", "competitor": "Acme", "type": "pricing",
        "headline": "New pricing page", "evidence_ids": ["ev-1"],
        "claim_ref": "pricing:acme",
    }]


def test_same_headline_different_competitor_or_type_is_new():
    prev = [_sig(payload={"event": "Raised a round"})]
    assert compute_delta(prev, [_sig(competitor="Umbrella", payload={"event": "Raised a round"})])
    assert compute_delta(prev, [_sig(type="launch", payload={"event": "Raised a round"})])


def test_headline_in_output_is_raw_not_normalized():
    out = compute_delta([], [_sig(payload={"event": "BIG News: $1B!!"})])
    assert out[0]["headline"] == "BIG News: $1B!!"


def test_claim_ref_lowercases_competitor():
    out = compute_delta([], [_sig(competitor="ClickUp", type="funding",
                                  payload={"event": "x"})])
    assert out[0]["claim_ref"] == "funding:clickup"
