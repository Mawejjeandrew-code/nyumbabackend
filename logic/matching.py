# ============================================
# NYUMBA — MATCHING / NOTIFICATIONS LOGIC
# File: app/matching.py
# ============================================
# Pure functions for deciding whether a listing matches
# a saved search, and for building notification messages.
# No database calls, no SMS/email sending here — same
# pattern as ranking.py and verification.py. The actual
# database query (find_matching_saved_searches) lives in
# SQL because it needs to run as one query, not a Python
# loop — but this file lets you unit test the MATCHING
# RULE itself, independent of the database.
# ============================================

from typing import Optional, TypedDict


class SavedSearch(TypedDict, total=False):
    id: str
    tenant_phone: str
    tenant_email: Optional[str]
    tenant_name: Optional[str]
    area: Optional[str]
    min_price_ugx: Optional[int]
    max_price_ugx: Optional[int]
    bedrooms: Optional[int]
    required_amenities: list[str]
    active: bool


class Listing(TypedDict, total=False):
    id: str
    title: str
    area: str
    price_ugx: int
    bedrooms: int
    amenities: list[str]
    verification_status: str


def listing_matches_search(listing: Listing, search: SavedSearch) -> bool:
    """
    The matching rule itself — mirrors find_matching_saved_searches()
    in SQL exactly, so this is testable in isolation. A listing
    matches a saved search if:
      - the listing is verified
      - area matches (or the tenant didn't specify one)
      - price falls within the tenant's stated range
      - bedrooms match (or unspecified)
      - the listing has ALL amenities the tenant required
    """
    if listing.get("verification_status") != "verified":
        return False

    if not search.get("active", True):
        return False

    area = search.get("area")
    if area is not None and area != listing.get("area"):
        return False

    min_price = search.get("min_price_ugx")
    if min_price is not None and listing.get("price_ugx", 0) < min_price:
        return False

    max_price = search.get("max_price_ugx")
    if max_price is not None and listing.get("price_ugx", 0) > max_price:
        return False

    bedrooms = search.get("bedrooms")
    if bedrooms is not None and listing.get("bedrooms") != bedrooms:
        return False

    required = search.get("required_amenities") or []
    listing_amenities = set(listing.get("amenities") or [])
    if not set(required).issubset(listing_amenities):
        return False

    return True


def find_matches(listing: Listing, searches: list[SavedSearch]) -> list[SavedSearch]:
    """
    Given one listing and a list of candidate saved searches,
    returns only the ones that actually match. This is the
    Python-side mirror of the SQL function — useful for unit
    testing the rule, or for an admin "preview matches" tool
    without writing to the database.
    """
    return [s for s in searches if listing_matches_search(listing, s)]


# ──────────────────────────────────────────
# MESSAGE BUILDERS
# ──────────────────────────────────────────

def build_sms_message(listing: Listing, tenant_name: Optional[str] = None) -> str:
    """Builds the SMS text sent when a match fires. Kept short —
    SMS has a practical length limit and Africa's Talking bills
    per segment."""
    greeting = f"Hi {tenant_name}, " if tenant_name else "Hi, "
    price = f"{listing.get('price_ugx', 0):,}"
    return (
        f"Nyumba: {greeting}a new verified listing matches your search — "
        f"{listing.get('title', 'a house')} in {listing.get('area', '')}, "
        f"UGX {price}/mo. No broker. View it now on Nyumba."
    )


def build_email_subject(listing: Listing) -> str:
    return f"New match: {listing.get('title', 'A house')} in {listing.get('area', '')}"


def build_email_body(listing: Listing, tenant_name: Optional[str] = None) -> str:
    """Plain-text email body. Keep this simple for v1 — an HTML
    template can come later once the basic flow is proven."""
    greeting = f"Hi {tenant_name}," if tenant_name else "Hi,"
    price = f"{listing.get('price_ugx', 0):,}"
    amenities = ", ".join(listing.get("amenities") or []) or "see listing for details"
    return (
        f"{greeting}\n\n"
        f"A new verified listing matches your saved search on Nyumba:\n\n"
        f"{listing.get('title', 'House')}\n"
        f"Area: {listing.get('area', '')}\n"
        f"Price: UGX {price}/month\n"
        f"Bedrooms: {listing.get('bedrooms', 'N/A')}\n"
        f"Amenities: {amenities}\n\n"
        f"This listing has been verified by our team — no broker, "
        f"no commission, direct contact with the landlord.\n\n"
        f"— Nyumba"
    )