"""Stats Node — every number is a row count (Rivalyze_Stats_Node.md QA cases).

TC-ST01 evidence_count == number of evidence rows
TC-ST02 source_type_breakdown sums to evidence_count
TC-ST03 competitors_with_complaints <= competitors_analyzed and matches a manual count
TC-ST04 corroboration_rate: 3 claims (1, 2, 2 sources) -> 67%
TC-ST05 empty run -> zeros/None, no crash, no division error
TC-ST06 report validates with the stats field absent (additive proof)
"""
from datetime import date, timedelta

from app.core.stats import compute_stats
from app.models import CompetitiveReport, ReportStats, Swot


def _ev(claim_ref, source_type, competitor="Acme", source_date=""):
    # shape of an evidence_index value (what the strategist passes in)
    return {"claim_ref": claim_ref, "type": source_type, "competitor": competitor,
            "source_date": source_date, "url": "https://u", "agent": source_type}


def _sig(competitor, type_):
    return {"agent": "review", "competitor": competitor, "type": type_, "payload": {}}


# ------------------------------- TC-ST01 -------------------------------
def test_evidence_count_equals_row_count():
    ev = [_ev("news:acme", "news"), _ev("pricing:acme", "pricing"), _ev("review:acme", "review")]
    out = compute_stats(ev, [], ["Acme"], {}, [])
    assert out["evidence_count"] == len(ev) == 3


# ------------------------------- TC-ST02 -------------------------------
def test_source_type_breakdown_sums_to_evidence_count():
    ev = [_ev("news:a", "news"), _ev("news:b", "news"), _ev("pricing:a", "pricing"),
          _ev("review:a", "review"), _ev("web:a", "web")]
    out = compute_stats(ev, [], ["Acme"], {}, [])
    assert sum(out["source_type_breakdown"].values()) == out["evidence_count"] == 5
    assert out["source_type_breakdown"]["news"] == 2
    assert out["source_type_breakdown"]["document"] == 0   # canonical slice, seeded to 0


# ------------------------------- TC-ST03 -------------------------------
def test_competitors_with_complaints_bounded_and_correct():
    competitors = ["Swiggy", "Zomato", "Dineout", "Burrp"]
    signals = [_sig("Swiggy", "complaint"), _sig("Swiggy", "pricing"),
               _sig("Zomato", "complaint"), _sig("Dineout", "launch"),
               _sig("Ghost Co", "complaint")]   # not a confirmed rival -> excluded
    out = compute_stats([], signals, competitors, {}, [])
    assert out["competitors_with_complaints"] == 2                  # Swiggy + Zomato only
    assert out["competitors_with_complaints"] <= out["competitors_analyzed"]


def test_complaints_match_confirmed_set_case_insensitively():
    out = compute_stats([], [_sig("swiggy", "complaint")], ["Swiggy"], {}, [])
    assert out["competitors_with_complaints"] == 1                  # casing must not drop it


# ------------------------------- TC-ST04 -------------------------------
def test_corroboration_rate_two_of_three_claims():
    # claim A: 1 source, claim B: 2 sources, claim C: 2 sources -> 2/3 corroborated
    ev = [_ev("news:a", "news"),
          _ev("pricing:b", "pricing"), _ev("pricing:b", "pricing"),
          _ev("review:c", "review"), _ev("review:c", "review")]
    out = compute_stats(ev, [], ["Acme"], {}, [])
    assert out["corroboration_rate"] == 67                          # round(100 * 2/3)


# ------------------------------- TC-ST05 -------------------------------
def test_empty_run_is_all_zeros_none_no_crash():
    out = compute_stats([], [], [], {}, [])
    assert out["evidence_count"] == 0
    assert out["competitors_analyzed"] == 0
    assert out["sources_per_competitor"] == {}
    assert sum(out["source_type_breakdown"].values()) == 0
    assert out["signals_by_type"] == {}
    assert out["competitors_with_complaints"] == 0
    assert out["sentiment_spread"] == {"POSITIVE": 0, "NEUTRAL": 0, "NEGATIVE": 0}
    assert out["avg_confidence"] is None                           # no recs -> None, not 0/0
    assert out["freshest_signal_days"] is None
    assert out["corroboration_rate"] is None                       # no claims -> None (no div-by-zero)
    # and it must construct a valid ReportStats
    assert ReportStats(**out).evidence_count == 0


# ------------------------------- TC-ST06 -------------------------------
def test_report_validates_without_stats_field():
    rep = CompetitiveReport(company="Acme", threat_level="MEDIUM", executive_summary="x",
                            swot=Swot(), analysis_date="2026-07-05")
    assert rep.stats is None                                        # additive: absent is fine


# --------------------------- extra coverage ----------------------------
def test_sources_per_competitor_and_sentiment_and_confidence():
    ev = [_ev("news:swiggy", "news", competitor="Swiggy"),
          _ev("pricing:swiggy", "pricing", competitor="Swiggy"),
          _ev("news:zomato", "news", competitor="Zomato")]
    sentiment = {"Swiggy": {"score": 0.2, "label": "NEGATIVE"},
                 "Zomato": {"score": 0.8, "label": "POSITIVE"}}
    recs = [{"confidence": 0.9}, {"confidence": 0.5}]
    out = compute_stats(ev, [], ["Swiggy", "Zomato"], sentiment, recs)
    assert out["sources_per_competitor"] == {"Swiggy": 2, "Zomato": 1}
    assert out["sentiment_spread"] == {"POSITIVE": 1, "NEUTRAL": 0, "NEGATIVE": 1}
    assert out["avg_confidence"] == 0.7                            # mean(0.9, 0.5)


def test_freshest_signal_days_picks_newest_parseable_date():
    today = date.today()
    ev = [_ev("news:a", "news", source_date=(today - timedelta(days=10)).isoformat()),
          _ev("news:b", "news", source_date=(today - timedelta(days=3)).isoformat()),
          _ev("news:c", "news", source_date="not a date")]        # unparseable -> ignored
    out = compute_stats(ev, [], ["Acme"], {}, [])
    assert out["freshest_signal_days"] == 3


def test_competitor_fallback_via_claim_ref_slug():
    # EvidenceRow-style rows carry NO competitor field -> fall back to claim_ref slug.
    # multi-word name: claim_ref uses the hyphen slug "uber-eats".
    ev = [{"claim_ref": "news:uber-eats", "source_type": "news", "source_date": ""}]
    out = compute_stats(ev, [], ["Uber Eats"], {}, [])
    assert out["sources_per_competitor"] == {"Uber Eats": 1}
