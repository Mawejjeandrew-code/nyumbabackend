

from datetime import datetime, timedelta, timezone
from typing import Optional, TypedDict


VALID_STATUSES = ["pending", "in_review", "verified", "rejected", "needs_info"]

# THE STATE MACHINE 
ALLOWED_TRANSITIONS = {
    "pending": ["in_review", "rejected"],
    "in_review": ["verified", "rejected", "needs_info"],
    "needs_info": ["in_review", "rejected"],   # landlord resubmits info -> back to review
    "rejected": ["pending"],                     # resubmission re-enters the queue from scratch
    "verified": [],                               # verified is terminal under normal flow
}

VERIFICATION_WINDOW_HOURS = 24
MAX_RESUBMISSIONS = 3


class Listing(TypedDict, total=False):
    id: str
    verification_status: str
    verification_deadline: Optional[datetime]
    resubmission_count: int
    assigned_agent_id: Optional[str]


class Agent(TypedDict, total=False):
    id: str
    active: bool
    current_workload: int
    latitude: float
    longitude: float


# STATE MACHINE FUNCTIONS


def can_transition(from_status: str, to_status: str) -> bool:
    """Checks whether moving a listing from one status to another
    is a legal transition. Always check this before writing to the
    database — never trust the caller to have done so."""
    if from_status not in VALID_STATUSES or to_status not in VALID_STATUSES:
        return False
    return to_status in ALLOWED_TRANSITIONS.get(from_status, [])


def get_valid_next_states(current_status: str) -> list[str]:
    """Returns the list of valid next states from the current one —
    useful for rendering the right action buttons in an agent's
    mobile view."""
    return ALLOWED_TRANSITIONS.get(current_status, [])

# DEADLINE / ESCALATION LOGIC


def compute_deadline(submitted_at: Optional[datetime] = None) -> datetime:
    """Computes the deadline timestamp for a freshly submitted listing.
    Mirrors the database trigger (set_verification_deadline) — kept
    here too so the same rule is testable without a database."""
    submitted_at = submitted_at or datetime.now(timezone.utc)
    return submitted_at + timedelta(hours=VERIFICATION_WINDOW_HOURS)


def is_overdue(listing: Listing, now: Optional[datetime] = None) -> bool:
    """Returns True if a listing is past its verification deadline
    and hasn't reached a resolved state yet."""
    now = now or datetime.now(timezone.utc)
    unresolved_statuses = ["pending", "in_review", "needs_info"]
    if listing.get("verification_status") not in unresolved_statuses:
        return False
    deadline = listing.get("verification_deadline")
    if not deadline:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline < now


def hours_overdue(listing: Listing, now: Optional[datetime] = None) -> float:
    """How many hours overdue a listing is. Returns 0 if not overdue.
    Useful for sorting an escalation queue worst-first."""
    now = now or datetime.now(timezone.utc)
    if not is_overdue(listing, now):
        return 0.0
    deadline = listing["verification_deadline"]
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    diff_seconds = (now - deadline).total_seconds()
    return round(diff_seconds / 3600, 1)

#resubmission

def can_resubmit(listing: Listing) -> bool:
    """Decides whether a rejected listing is allowed to be resubmitted,
    or whether it's hit the cap and needs a human to manually review
    the situation instead of letting it loop indefinitely."""
    if listing.get("verification_status") != "rejected":
        return False
    return listing.get("resubmission_count", 0) < MAX_RESUBMISSIONS


def build_resubmission_update(listing: Listing, now: Optional[datetime] = None) -> dict:
    """Builds the field update dict for a resubmission — resets to
    'pending' so it re-enters the queue and gets a fresh agent
    assignment + fresh 24-hour deadline, while incrementing the
    count so it can't loop forever."""
    if not can_resubmit(listing):
        raise ValueError(
            f"Listing has reached the maximum of {MAX_RESUBMISSIONS} "
            f"resubmissions and requires manual review."
        )
    now = now or datetime.now(timezone.utc)
    return {
        "verification_status": "pending",
        "resubmission_count": listing.get("resubmission_count", 0) + 1,
        "submitted_at": now,
        "verification_deadline": compute_deadline(now),
        "rejection_reason": None,
        "assigned_agent_id": None,
        "escalated": False,
    }

def distance_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Simple Euclidean distance between two lat/long points.
    Good enough at city scale — not for cross-country distances."""
    return ((lat1 - lat2) ** 2 + (lng1 - lng2) ** 2) ** 0.5


def find_nearest_available_agent(
    listing_lat: float,
    listing_lng: float,
    agents: list[Agent],
    max_workload: int = 8,
) -> Optional[Agent]:
    """Given a listing's coordinates and a list of candidate agents,
    returns the nearest one with workload under the cap, or None
    if nobody is available."""
    eligible = [a for a in agents if a.get("active") and a.get("current_workload", 0) < max_workload]
    if not eligible:
        return None

    nearest = None
    nearest_distance = None
    for agent in eligible:
        d = distance_between(listing_lat, listing_lng, agent["latitude"], agent["longitude"])
        if nearest is None or d < nearest_distance:
            nearest = agent
            nearest_distance = d

    return nearest