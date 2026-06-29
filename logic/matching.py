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

class Listing(TypedDict, total=False):
    id: str
    title: str
    area: str
    price_ugx: int
    bedrooms: int
    amenities: list[str]
    verification_status: str

def matches_search(listing: Listing, search: SavedSearch) -> bool:
    """
    The matching rule itself - mirrors find_matching_saved_searches()
    in sql exctly, so this is testable in isolation. A listing matches a saved
    search if:
       -the listing is verified
       - area matches (or the tenant's didn't specify one)
       - price falls within the tenant[is stated range]
       bedrooms match (or unspecified)
       - the listing has  ALL amenities the tenant required
    """

    if listing.get("verification_status") != "verified":
        return False
    
    if not search.get("active", True):
        return False
    area = search.get("area")
    if area is not None and area != listing.get("area"):
        return False
    
    required = search.get("required_amenities") or []
    listing_amenities = set(listing.get("amenities") or [])
    if not set(required).issubset(listing_amenities):
        return False
    return True


def find_matches(listing:Listing, searches: list[SavedSearch]) -> list[SavedSearch]:

    """
    given one listing and a list of candidates saved searches,
    returns only the ones that actually match. This is the python-side mirror of the SQL function - useful for unut
    testing the rule, or for an admin " preview matches" tool
    without writing to the database.
    """
    return [s for s in searches if matches_search(listing, s)]


    
    



