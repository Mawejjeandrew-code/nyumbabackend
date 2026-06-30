# ============================================
# NYUMBA — MATCHING LOGIC TESTS
# File: app/test_matching.py
# ============================================
# Run with: pytest app/test_matching.py -v
# ============================================

from logic.matching import (
    listing_matches_search,
    find_matches,
    build_sms_message,
    build_email_subject,
    build_email_body,
)


def make_listing(**overrides):
    base = {
        "id": "listing-1",
        "title": "2BR Self-contained",
        "area": "Ntinda",
        "price_ugx": 450000,
        "bedrooms": 2,
        "amenities": ["water", "security", "parking"],
        "verification_status": "verified",
    }
    base.update(overrides)
    return base


def make_search(**overrides):
    base = {
        "id": "search-1",
        "tenant_phone": "+256701234567",
        "tenant_email": "sarah@example.com",
        "tenant_name": "Sarah",
        "area": "Ntinda",
        "min_price_ugx": None,
        "max_price_ugx": None,
        "bedrooms": None,
        "required_amenities": [],
        "active": True,
    }
    base.update(overrides)
    return base


def test_unverified_listing_never_matches():
    listing = make_listing(verification_status="pending")
    search = make_search()
    assert listing_matches_search(listing, search) is False


def test_inactive_search_never_matches():
    listing = make_listing()
    search = make_search(active=False)
    assert listing_matches_search(listing, search) is False


def test_area_must_match_when_specified():
    listing = make_listing(area="Ntinda")
    matching_search = make_search(area="Ntinda")
    non_matching_search = make_search(area="Kisaasi")
    assert listing_matches_search(listing, matching_search) is True
    assert listing_matches_search(listing, non_matching_search) is False


def test_area_unspecified_matches_anything():
    listing = make_listing(area="Naguru")
    search = make_search(area=None)
    assert listing_matches_search(listing, search) is True


def test_price_range_filtering():
    listing = make_listing(price_ugx=450000)
    within_range = make_search(min_price_ugx=300000, max_price_ugx=500000)
    too_expensive = make_search(min_price_ugx=300000, max_price_ugx=400000)
    too_cheap = make_search(min_price_ugx=500000, max_price_ugx=600000)
    assert listing_matches_search(listing, within_range) is True
    assert listing_matches_search(listing, too_expensive) is False
    assert listing_matches_search(listing, too_cheap) is False


def test_bedrooms_must_match_exactly_when_specified():
    listing = make_listing(bedrooms=2)
    matching = make_search(bedrooms=2)
    non_matching = make_search(bedrooms=3)
    unspecified = make_search(bedrooms=None)
    assert listing_matches_search(listing, matching) is True
    assert listing_matches_search(listing, non_matching) is False
    assert listing_matches_search(listing, unspecified) is True


def test_amenities_must_be_subset():
    listing = make_listing(amenities=["water", "security", "parking"])
    satisfied = make_search(required_amenities=["water", "security"])
    not_satisfied = make_search(required_amenities=["water", "wifi"])  # listing lacks wifi
    no_requirement = make_search(required_amenities=[])
    assert listing_matches_search(listing, satisfied) is True
    assert listing_matches_search(listing, not_satisfied) is False
    assert listing_matches_search(listing, no_requirement) is True


def test_find_matches_filters_a_list_correctly():
    listing = make_listing(area="Ntinda", price_ugx=450000, bedrooms=2)
    searches = [
        make_search(id="s1", area="Ntinda"),                     # matches
        make_search(id="s2", area="Kisaasi"),                    # area mismatch
        make_search(id="s3", area="Ntinda", bedrooms=3),         # bedroom mismatch
        make_search(id="s4", area=None),                          # matches (no area filter)
    ]
    matches = find_matches(listing, searches)
    matched_ids = {m["id"] for m in matches}
    assert matched_ids == {"s1", "s4"}


def test_sms_message_includes_key_details():
    listing = make_listing()
    msg = build_sms_message(listing, tenant_name="Sarah")
    assert "Sarah" in msg
    assert "Ntinda" in msg
    assert "450,000" in msg
    assert "No broker" in msg


def test_sms_message_handles_missing_name():
    listing = make_listing()
    msg = build_sms_message(listing, tenant_name=None)
    assert "Hi," in msg  # falls back gracefully, no "None" leaking into the message


def test_email_subject_and_body():
    listing = make_listing()
    subject = build_email_subject(listing)
    body = build_email_body(listing, tenant_name="Sarah")
    assert "Ntinda" in subject
    assert "Sarah" in body
    assert "450,000" in body
    assert "water, security, parking" in body