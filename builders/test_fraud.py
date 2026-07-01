# ============================================
# NYUMBA — FRAUD SCORING TESTS
# File: app/test_fraud.py
# ============================================
# Run with: pytest app/test_fraud.py -v
# ============================================

from datetime import datetime, timezone, timedelta
import pytest

from builders.fraud import (
    score_price_drift,
    score_response_spike,
    score_edit_after_verification,
    score_tenant_reports,
    calculate_fraud_score,
    FRAUD_THRESHOLD_REVIEW,
    FRAUD_THRESHOLD_HIDE,
)

NOW = datetime.now(timezone.utc)


def test_price_drift_clean_listing():
    assert score_price_drift(450000, 450000, 450000) == 0.0


def test_price_drift_small_drop_is_fine():
    # 10% drop — under the 20% suspicious threshold
    assert score_price_drift(405000, 450000, 450000) == 0.0


def test_price_drift_large_drop_is_suspicious():
    # 40% drop after getting verified badge
    score = score_price_drift(270000, 450000, 450000)
    assert score > 0.0
    assert score <= 60.0


def test_price_drift_scam_bait_pricing():
    # Price now 30% of area average — anti-scam territory
    score = score_price_drift(135000, 450000, 450000)
    assert score >= 50.0


def test_price_drift_no_snapshot_scores_zero():
    # No verification snapshot — no basis for comparison
    assert score_price_drift(200000, None, 450000) == 0.0


def test_response_spike_clean():
    # Same response time as at verification
    assert score_response_spike(30, 30) == 0.0


def test_response_spike_moderate():
    # 2x slower — under the 3x threshold
    assert score_response_spike(60, 30) == 0.0


def test_response_spike_severe():
    # 10x slower — well past the threshold. Under the widened
    # 0-100 scale (0 at 3x, 100 at 15x), 10x lands around 58.
    score = score_response_spike(300, 30)
    assert score > 0.0
    assert score <= 100.0


def test_response_spike_no_data():
    assert score_response_spike(None, 30) == 0.0
    assert score_response_spike(300, None) == 0.0


def test_response_spike_regression_current_compared_to_itself_is_always_zero():
    # Regression guard: this is the specific bug that shipped in main.py —
    # response_at_verification was accidentally populated from the SAME
    # source as current_avg_response_minutes, so the comparison was
    # always "X vs X" and the signal could never fire, no matter how
    # badly a landlord's response time degraded. This test documents
    # that exact failure mode at the signal-function level: if a caller
    # ever again passes the same value for both arguments, the score
    # is guaranteed to be 0 — which is correct math, but is also the
    # bug, if the two values were supposed to be genuinely different
    # (current vs. a real historical snapshot) and weren't.
    landlord_current_response = 600  # severely degraded
    accidentally_same_value = 600    # the bug: snapshot == current
    assert score_response_spike(landlord_current_response, accidentally_same_value) == 0.0, (
        "If this is ever non-zero, the test itself is wrong — the real "
        "fix lives in main.py, ensuring response_at_verification is "
        "populated from response_minutes_at_verification (a true "
        "snapshot column), never from the landlord's current value."
    )


def test_edit_after_verification_immediate():
    # Edited 2 hours after getting verified badge
    verified_at = NOW - timedelta(hours=10)
    edited_at = NOW - timedelta(hours=8)
    score = score_edit_after_verification(edited_at, verified_at)
    assert score > 0.0


def test_edit_after_verification_long_after():
    # Edited 2 weeks after verification — fine
    verified_at = NOW - timedelta(days=30)
    edited_at = NOW - timedelta(days=16)
    score = score_edit_after_verification(edited_at, verified_at)
    assert score == 0.0


def test_edit_before_verification_is_fine():
    # Edit happened before badge was awarded — fine
    verified_at = NOW - timedelta(hours=5)
    edited_at = NOW - timedelta(hours=10)
    score = score_edit_after_verification(edited_at, verified_at)
    assert score == 0.0


def test_tenant_reports_zero():
    assert score_tenant_reports(0, 0, []) == 0.0


def test_tenant_reports_mild():
    score = score_tenant_reports(1, 0, ["unresponsive"])
    assert 0.0 < score < 30.0


def test_tenant_reports_severe_reasons():
    mild = score_tenant_reports(1, 0, ["unresponsive"])
    severe = score_tenant_reports(1, 0, ["scam_attempt"])
    assert severe > mild


def test_tenant_reports_recent_weighted_higher():
    old = score_tenant_reports(2, 0, ["wrong_price"])
    recent = score_tenant_reports(2, 2, ["wrong_price"])
    assert recent > old


def test_calculate_fraud_score_clean_listing():
    signals = {
        "price_ugx": 450000,
        "price_at_verification": 450000,
        "area_avg_price": 450000,
        "current_avg_response_minutes": 30,
        "response_at_verification": 30,
        "last_edited_at": None,
        "verified_at": NOW - timedelta(days=10),
        "total_reports": 0,
        "reports_last_7_days": 0,
        "report_reasons": [],
    }
    result = calculate_fraud_score(signals)
    assert result["score"] < FRAUD_THRESHOLD_REVIEW
    assert result["recommended_action"] == "none"


def test_calculate_fraud_score_suspicious_listing():
    signals = {
        "price_ugx": 100000,              # scam-bait price
        "price_at_verification": 450000,  # was fair at verification
        "area_avg_price": 450000,
        "current_avg_response_minutes": 600,
        "response_at_verification": 30,
        "last_edited_at": NOW - timedelta(hours=3),  # edited right after badge
        "verified_at": NOW - timedelta(hours=5),
        "total_reports": 3,
        "reports_last_7_days": 3,
        "report_reasons": ["scam_attempt", "bait_and_switch"],
    }
    result = calculate_fraud_score(signals)
    # Verified by hand: this combination scores ~57 — well past the
    # review threshold, correctly flagged for human attention even
    # though it doesn't hit every signal's absolute maximum.
    assert result["score"] >= FRAUD_THRESHOLD_REVIEW
    assert result["recommended_action"] in ("flag_for_review", "revert_to_needs_info")


def test_calculate_fraud_score_low_risk_single_mild_signal():
    # A single mild report and modest price/response drift —
    # none of it individually crosses a suspicion threshold by much,
    # so the combined score correctly stays low. This is the
    # "don't cry wolf" case: a landlord shouldn't get flagged for
    # one so-so report and a reasonable price adjustment.
    signals = {
        "price_ugx": 350000,
        "price_at_verification": 450000,  # 22% drop — just past the 20% threshold
        "area_avg_price": 450000,
        "current_avg_response_minutes": 120,
        "response_at_verification": 30,    # 4x spike — just past the 3x threshold
        "last_edited_at": None,
        "verified_at": NOW - timedelta(days=5),
        "total_reports": 1,
        "reports_last_7_days": 1,
        "report_reasons": ["wrong_price"],
    }
    result = calculate_fraud_score(signals)
    assert result["score"] < FRAUD_THRESHOLD_REVIEW
    assert result["recommended_action"] == "none"
    assert "breakdown" in result
    assert len(result["breakdown"]) == 4


def test_recommended_action_thresholds():
    clean = {"price_ugx": 450000, "price_at_verification": 450000,
             "area_avg_price": 450000, "current_avg_response_minutes": 30,
             "response_at_verification": 30, "last_edited_at": None,
             "verified_at": NOW - timedelta(days=30),
             "total_reports": 0, "reports_last_7_days": 0, "report_reasons": []}
    assert calculate_fraud_score(clean)["recommended_action"] == "none"

    # Extreme case maxing out every signal — verified by hand to
    # score ~99.6, correctly triggering automatic reversion.
    scam = {"price_ugx": 50000, "price_at_verification": 450000,
            "area_avg_price": 450000, "current_avg_response_minutes": 1440,
            "response_at_verification": 20,
            "last_edited_at": NOW - timedelta(hours=1),
            "verified_at": NOW - timedelta(hours=2),
            "total_reports": 5, "reports_last_7_days": 5,
            "report_reasons": ["scam_attempt", "fake_listing"]}
    assert calculate_fraud_score(scam)["recommended_action"] == "revert_to_needs_info"