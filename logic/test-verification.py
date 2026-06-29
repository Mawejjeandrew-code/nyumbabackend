# ============================================
# NYUMBA — VERIFICATION WORKFLOW TESTS (Python)
# File: app/test_verification.py
# ============================================
# Run with: pytest app/test_verification.py -v
# ============================================

from datetime import datetime, timezone
import pytest

from app.verification import (
    can_transition,
    get_valid_next_states,
    compute_deadline,
    is_overdue,
    hours_overdue,
    can_resubmit,
    build_resubmission_update,
    distance_between,
    find_nearest_available_agent,
    MAX_RESUBMISSIONS,
)


def test_state_machine_transitions():
    assert can_transition("pending", "in_review") is True
    assert can_transition("pending", "rejected") is True
    assert can_transition("pending", "verified") is False, "must pass through in_review"
    assert can_transition("in_review", "verified") is True
    assert can_transition("in_review", "needs_info") is True
    assert can_transition("needs_info", "in_review") is True
    assert can_transition("rejected", "pending") is True
    assert can_transition("rejected", "verified") is False, "must re-enter queue first"
    assert can_transition("verified", "pending") is False, "verified is terminal"
    assert can_transition("bogus", "verified") is False, "unknown status returns False, not a crash"


def test_valid_next_states():
    assert "in_review" in get_valid_next_states("pending")
    assert get_valid_next_states("verified") == []


def test_deadline_computation():
    submitted = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    deadline = compute_deadline(submitted)
    assert (deadline - submitted).total_seconds() == 24 * 3600


def test_overdue_detection():
    submitted = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    deadline = compute_deadline(submitted)
    now = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)  # 26 hours after submission

    overdue_listing = {"verification_status": "pending", "verification_deadline": deadline}
    fresh_listing = {"verification_status": "pending", "verification_deadline": compute_deadline(now)}
    verified_listing = {"verification_status": "verified", "verification_deadline": deadline}

    assert is_overdue(overdue_listing, now) is True
    assert is_overdue(fresh_listing, now) is False
    assert is_overdue(verified_listing, now) is False, "verified listing is never overdue"


def test_hours_overdue_calculation():
    submitted = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    deadline = compute_deadline(submitted)
    now = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)  # 2 hours past deadline

    overdue_listing = {"verification_status": "pending", "verification_deadline": deadline}
    fresh_listing = {"verification_status": "pending", "verification_deadline": compute_deadline(now)}

    assert hours_overdue(overdue_listing, now) == 2.0
    assert hours_overdue(fresh_listing, now) == 0.0


def test_resubmission_logic():
    rejected = {"verification_status": "rejected", "resubmission_count": 0}
    maxed_out = {"verification_status": "rejected", "resubmission_count": MAX_RESUBMISSIONS}
    pending = {"verification_status": "pending", "resubmission_count": 0}

    assert can_resubmit(rejected) is True
    assert can_resubmit(maxed_out) is False
    assert can_resubmit(pending) is False, "wrong state entirely"

    now = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)
    update = build_resubmission_update(rejected, now)
    assert update["verification_status"] == "pending"
    assert update["resubmission_count"] == 1
    assert update["assigned_agent_id"] is None
    assert update["escalated"] is False

    with pytest.raises(ValueError):
        build_resubmission_update(maxed_out, now)


def test_proximity_agent_assignment():
    assert abs(distance_between(0, 0, 3, 4) - 5) < 0.0001, "3-4-5 triangle"

    agents = [
        {"id": "a1", "active": True, "current_workload": 2, "latitude": 0.347, "longitude": 32.582},  # close
        {"id": "a2", "active": True, "current_workload": 1, "latitude": 0.500, "longitude": 32.700},  # far
        {"id": "a3", "active": False, "current_workload": 0, "latitude": 0.348, "longitude": 32.583}, # closest but inactive
        {"id": "a4", "active": True, "current_workload": 9, "latitude": 0.349, "longitude": 32.584},  # close but maxed out
    ]
    listing_lat, listing_lng = 0.3476, 32.5825

    nearest = find_nearest_available_agent(listing_lat, listing_lng, agents, max_workload=8)
    assert nearest["id"] == "a1", "nearest ACTIVE, non-maxed-out agent wins"

    none_available = find_nearest_available_agent(listing_lat, listing_lng, [], max_workload=8)
    assert none_available is None