

import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

from app.ranking import rank_listings, DEFAULT_WEIGHTS
from app.verification import (
    can_transition,
    can_resubmit,
    build_resubmission_update,
)
from logic.matching import build_sms_message, build_email_subject, build_email_body
from builders.sms import send_match_sms
from builders.email import send_match_email
from builders.fraud import calculate_fraud_score, FRAUD_THRESHOLD_REVIEW, FRAUD_THRESHOLD_HIDE
from app.auth import validate_signup_input, normalize_phone, phone_to_pseudo_email, is_valid_uganda_phone


app = FastAPI(title="Nyumba Backend Service")

# ── CORS — allow your Next.js frontend to call this service ──
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    os.environ.get("FRONTEND_URL", ""),  # e.g. https://nyumba-waitlist.vercel.app
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in ALLOWED_ORIGINS if o],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Supabase client (service role — server-to-server only) ──
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def require_supabase():
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured.")


def run_matching_for_listing(listing_id: str) -> dict:
    """
    The core matching trigger. Called the moment a listing becomes
    'verified'. Finds every saved search that matches (via the SQL
    function — one query, not a Python loop over every search),
    sends SMS + email to each, and logs every send in
    notifications_sent so the same match never fires twice.

    Notifies immediately, every time — no batching, per the decision
    that tenants would rather hear about every match right away.
    """
    if supabase is None:
        return {"matched": 0, "error": "Supabase not configured."}

    # ── fetch the listing itself, for the message content ──
    listing_result = (
        supabase.table("listings")
        .select("id, title, area, price_ugx, bedrooms, amenities, verification_status")
        .eq("id", listing_id)
        .single()
        .execute()
    )
    if not listing_result.data:
        return {"matched": 0, "error": "Listing not found."}
    listing = listing_result.data

    # ── run the matching query — one database call ──
    matches_result = supabase.rpc(
        "find_matching_saved_searches", {"p_listing_id": listing_id}
    ).execute()
    matches = matches_result.data or []

    sent_count = 0
    for match in matches:
        tenant_name = match.get("tenant_name")
        sms_text = build_sms_message(listing, tenant_name)
        email_subject = build_email_subject(listing)
        email_body = build_email_body(listing, tenant_name)

        # ── SMS ──
        if match.get("tenant_phone"):
            sms_result = send_match_sms(match["tenant_phone"], sms_text)
            supabase.table("notifications_sent").insert({
                "saved_search_id": match["saved_search_id"],
                "listing_id": listing_id,
                "channel": "sms",
                "status": "sent" if sms_result.get("success") else "failed",
            }).execute()
            if sms_result.get("success"):
                sent_count += 1

        # ── Email ──
        if match.get("tenant_email"):
            email_result = send_match_email(match["tenant_email"], email_subject, email_body)
            supabase.table("notifications_sent").insert({
                "saved_search_id": match["saved_search_id"],
                "listing_id": listing_id,
                "channel": "email",
                "status": "sent" if email_result.get("success") else "failed",
            }).execute()
            if email_result.get("success"):
                sent_count += 1

    return {"matched": len(matches), "notifications_sent": sent_count}


@app.get("/health")
def health():
    return {"status": "ok", "supabase_connected": supabase is not None}


@app.get("/search")
def search(
    area: Optional[str] = None,
    min_price: Optional[int] = Query(None, alias="minPrice"),
    max_price: Optional[int] = Query(None, alias="maxPrice"),
    bedrooms: Optional[int] = None,
    verified_only: bool = Query(False, alias="verifiedOnly"),
    amenities: Optional[str] = Query(None, description="comma-separated, e.g. water,security,parking"),
):
    """
    Tenant-facing search endpoint. Fetches listings matching filters
    from Supabase, ranks them with the Python algorithm, returns
    sorted results. Mirrors pages/api/search.js but in Python.

    Amenities are a HARD FILTER, not a ranking signal — a listing
    either has everything the tenant required, or it's excluded
    from results entirely. This deliberately mirrors search.js so
    the two services never disagree on what "matches" means.
    """
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured.")

    query = (
        supabase.table("listings")
        .select(
            "id, title, area, price_ugx, bedrooms, bathrooms, "
            "is_verified, photo_count, last_updated_at, landlord_id, amenities, "
            "landlords(id, name, avg_response_minutes)"
        )
        .eq("status", "live")
    )

    if area:
        query = query.eq("area", area)
    if min_price is not None:
        query = query.gte("price_ugx", min_price)
    if max_price is not None:
        query = query.lte("price_ugx", max_price)
    if bedrooms is not None:
        query = query.eq("bedrooms", bedrooms)
    if verified_only:
        query = query.eq("is_verified", True)
    if amenities:
        required = [a.strip() for a in amenities.split(",") if a.strip()]
        if required:
            query = query.contains("amenities", required)

    response = query.execute()
    listings = response.data or []

    if not listings:
        return {"results": [], "count": 0}

    # ── area average prices, for the price-competitiveness signal ──
    avg_response = supabase.table("area_avg_price").select("area, avg_price_ugx").execute()
    area_avg_map = {row["area"]: row["avg_price_ugx"] for row in (avg_response.data or [])}

    # ── flatten the joined landlord data ──
    for listing in listings:
        listing["landlord"] = listing.pop("landlords", None)
        # Supabase returns ISO date strings — ranking.py needs datetime objects
        if isinstance(listing.get("last_updated_at"), str):
            from datetime import datetime
            listing["last_updated_at"] = datetime.fromisoformat(
                listing["last_updated_at"].replace("Z", "+00:00")
            )

    ranked = rank_listings(listings, area_avg_map)

    # ── strip internal scoring breakdown before returning to tenants ──
    public_results = [
        {k: v for k, v in listing.items() if k != "_score_breakdown"}
        for listing in ranked
    ]

    return {"results": public_results, "count": len(public_results)}


@app.get("/admin/search-debug")
def search_debug(
    area: Optional[str] = None,
    min_price: Optional[int] = Query(None, alias="minPrice"),
    max_price: Optional[int] = Query(None, alias="maxPrice"),
):
    """
    Same as /search, but INCLUDES the score breakdown for each listing.
    Use this for an internal admin view to answer "why does this
    listing rank where it does" — never expose this route to tenants.
    """
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured.")

    query = (
        supabase.table("listings")
        .select(
            "id, title, area, price_ugx, bedrooms, bathrooms, "
            "is_verified, photo_count, last_updated_at, landlord_id, "
            "landlords(id, name, avg_response_minutes)"
        )
        .eq("status", "live")
    )
    if area:
        query = query.eq("area", area)
    if min_price is not None:
        query = query.gte("price_ugx", min_price)
    if max_price is not None:
        query = query.lte("price_ugx", max_price)

    response = query.execute()
    listings = response.data or []

    if not listings:
        return {"results": []}

    avg_response = supabase.table("area_avg_price").select("area, avg_price_ugx").execute()
    area_avg_map = {row["area"]: row["avg_price_ugx"] for row in (avg_response.data or [])}

    for listing in listings:
        listing["landlord"] = listing.pop("landlords", None)
        if isinstance(listing.get("last_updated_at"), str):
            from datetime import datetime
            listing["last_updated_at"] = datetime.fromisoformat(
                listing["last_updated_at"].replace("Z", "+00:00")
            )

    ranked = rank_listings(listings, area_avg_map)
    return {"results": ranked}


@app.get("/admin/weights")
def get_default_weights():
    """Returns the current default ranking weights — useful for an
    admin dashboard that lets you preview different weight settings."""
    return {"weights": DEFAULT_WEIGHTS}


# ============================================
# VERIFICATION WORKFLOW ENDPOINTS
# ============================================

class SubmitListingRequest(BaseModel):
    landlord_id: str
    title: str
    area: str
    price_ugx: int
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    amenities: list[str] = []
    latitude: float
    longitude: float


@app.post("/listings/submit")
def submit_listing(payload: SubmitListingRequest):
    """
    Called when a landlord finishes the listing wizard. Creates
    the listing as 'pending', then immediately tries to auto-assign
    the nearest available field agent via the SQL function.
    """
    require_supabase()

    insert_data = {
        "landlord_id": payload.landlord_id,
        "title": payload.title,
        "area": payload.area,
        "price_ugx": payload.price_ugx,
        "bedrooms": payload.bedrooms,
        "bathrooms": payload.bathrooms,
        "amenities": payload.amenities,
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "status": "draft",  # not searchable until verified
    }

    result = supabase.table("listings").insert(insert_data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create listing.")
    listing = result.data[0]

    # ── Immediately attempt proximity-based agent assignment ──
    agent_result = supabase.rpc(
        "assign_listing_to_agent", {"p_listing_id": listing["id"]}
    ).execute()
    agent_id = agent_result.data if agent_result.data else None

    return {
        "success": True,
        "listing": listing,
        "assigned_agent_id": agent_id,
        "message": (
            "Listing submitted and assigned to a field agent for verification."
            if agent_id
            else "Listing submitted. No agent available right now — it will be assigned shortly."
        ),
    }


class VerifyDecisionRequest(BaseModel):
    listing_id: str
    agent_id: Optional[str] = None
    new_status: str
    notes: Optional[str] = None
    rejection_reason: Optional[str] = None


@app.post("/listings/verify")
def verify_listing(payload: VerifyDecisionRequest):
    """
    Called from the field agent's mobile view when they finish
    inspecting a property. Enforces the state machine rules from
    app/verification.py — illegal transitions are rejected here,
    before they ever reach the database.
    """
    require_supabase()

    listing_result = (
        supabase.table("listings")
        .select("id, verification_status, assigned_agent_id, resubmission_count")
        .eq("id", payload.listing_id)
        .single()
        .execute()
    )
    if not listing_result.data:
        raise HTTPException(status_code=404, detail="Listing not found.")
    listing = listing_result.data

    # ── Enforce the state machine — this is the whole point ──
    if not can_transition(listing["verification_status"], payload.new_status):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot move from '{listing['verification_status']}' to '{payload.new_status}'.",
        )

    updates = {"verification_status": payload.new_status}
    if payload.new_status == "rejected" and payload.rejection_reason:
        updates["rejection_reason"] = payload.rejection_reason
    if payload.new_status == "verified":
        updates["status"] = "live"  # now searchable — this is the flag ranking.py reads

    supabase.table("listings").update(updates).eq("id", payload.listing_id).execute()

    # ── Free up the agent's workload slot if this listing is now resolved ──
    resolved_statuses = ["verified", "rejected"]
    if payload.new_status in resolved_statuses and listing.get("assigned_agent_id"):
        agent_result = (
            supabase.table("field_agents")
            .select("current_workload, total_verified")
            .eq("id", listing["assigned_agent_id"])
            .single()
            .execute()
        )
        if agent_result.data:
            agent = agent_result.data
            supabase.table("field_agents").update({
                "current_workload": max(0, agent["current_workload"] - 1),
                "total_verified": (
                    agent["total_verified"] + 1
                    if payload.new_status == "verified"
                    else agent["total_verified"]
                ),
            }).eq("id", listing["assigned_agent_id"]).execute()

    # ── Log the transition for the audit trail ──
    supabase.table("verification_log").insert({
        "listing_id": payload.listing_id,
        "agent_id": payload.agent_id or listing.get("assigned_agent_id"),
        "from_status": listing["verification_status"],
        "to_status": payload.new_status,
        "notes": payload.notes,
    }).execute()

    # ── Trigger matching the instant a listing becomes verified ──
    # This is the exact event point we decided on: notify ONLY
    # after verification clears, never before.
    matching_result = None
    if payload.new_status == "verified":
        matching_result = run_matching_for_listing(payload.listing_id)

    return {"success": True, "new_status": payload.new_status, "matching": matching_result}


class ResubmitRequest(BaseModel):
    listing_id: str


@app.post("/listings/resubmit")
def resubmit_listing(payload: ResubmitRequest):
    """
    A landlord whose listing was rejected can fix the issue and
    resubmit. Enforces the MAX_RESUBMISSIONS cap — after 3 attempts,
    this refuses and tells them to contact support instead of
    looping indefinitely.
    """
    require_supabase()

    listing_result = (
        supabase.table("listings")
        .select("id, verification_status, resubmission_count")
        .eq("id", payload.listing_id)
        .single()
        .execute()
    )
    if not listing_result.data:
        raise HTTPException(status_code=404, detail="Listing not found.")
    listing = listing_result.data

    if not can_resubmit(listing):
        raise HTTPException(
            status_code=403,
            detail="This listing has reached the maximum number of resubmissions. Please contact support.",
        )

    try:
        updates = build_resubmission_update(listing)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    # Supabase needs ISO strings, not datetime objects
    updates["submitted_at"] = updates["submitted_at"].isoformat()
    updates["verification_deadline"] = updates["verification_deadline"].isoformat()

    supabase.table("listings").update(updates).eq("id", payload.listing_id).execute()

    agent_result = supabase.rpc(
        "assign_listing_to_agent", {"p_listing_id": payload.listing_id}
    ).execute()
    agent_id = agent_result.data if agent_result.data else None

    supabase.table("verification_log").insert({
        "listing_id": payload.listing_id,
        "from_status": "rejected",
        "to_status": "pending",
        "notes": f"Resubmission #{updates['resubmission_count']}",
    }).execute()

    return {
        "success": True,
        "resubmission_count": updates["resubmission_count"],
        "assigned_agent_id": agent_id,
    }


@app.post("/cron/check-escalations")
def check_escalations(authorization: Optional[str] = Header(None)):
    """
    Call this every 15 minutes via an external scheduler
    (cron-job.org, Render Cron, etc). Finds listings past their
    24-hour deadline, marks them escalated, and logs an alert.
    """
    if authorization != f"Bearer {CRON_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    require_supabase()

    overdue_result = supabase.table("overdue_verifications").select("*").execute()
    overdue = overdue_result.data or []

    if not overdue:
        return {"escalated": 0, "message": "No overdue verifications."}

    ids = [l["id"] for l in overdue]
    supabase.table("listings").update({"escalated": True}).in_("id", ids).execute()

    supabase.table("verification_log").insert([
        {
            "listing_id": l["id"],
            "agent_id": l.get("assigned_agent_id"),
            "from_status": l["verification_status"],
            "to_status": l["verification_status"],
            "notes": f"Escalated — {l['hours_overdue']:.1f} hours overdue",
        }
        for l in overdue
    ]).execute()

    print(f"ESCALATION: {len(overdue)} listings overdue:", [
        {
            "title": l.get("title"),
            "area": l.get("area"),
            "hours_overdue": round(l["hours_overdue"], 1),
            "agent": l.get("agent_name") or "unassigned",
        }
        for l in overdue
    ])

    return {"escalated": len(overdue), "listings": overdue}


# ============================================
# MATCHING / NOTIFICATIONS ENDPOINTS
# ============================================

class SavedSearchRequest(BaseModel):
    tenant_phone: str
    tenant_email: Optional[str] = None
    tenant_name: Optional[str] = None
    area: Optional[str] = None
    min_price_ugx: Optional[int] = None
    max_price_ugx: Optional[int] = None
    bedrooms: Optional[int] = None
    required_amenities: list[str] = []


@app.post("/saved-searches")
def create_saved_search(payload: SavedSearchRequest):
    """
    A tenant registers a standing search — "tell me when something
    like this appears." This is what run_matching_for_listing()
    checks every newly-verified listing against.
    """
    require_supabase()

    if not payload.tenant_phone and not payload.tenant_email:
        raise HTTPException(status_code=400, detail="At least one of phone or email is required.")

    insert_data = {
        "tenant_phone": payload.tenant_phone,
        "tenant_email": payload.tenant_email,
        "tenant_name": payload.tenant_name,
        "area": payload.area,
        "min_price_ugx": payload.min_price_ugx,
        "max_price_ugx": payload.max_price_ugx,
        "bedrooms": payload.bedrooms,
        "required_amenities": payload.required_amenities,
    }
    result = supabase.table("saved_searches").insert(insert_data).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create saved search.")

    return {"success": True, "saved_search": result.data[0]}


class TriggerMatchingRequest(BaseModel):
    listing_id: str


@app.post("/admin/trigger-matching")
def trigger_matching_manually(payload: TriggerMatchingRequest):
    """
    Manually re-run matching for a specific listing — useful for
    testing the pipeline, or for re-notifying after a saved search
    was added post-verification. Does NOT bypass the verified-only
    rule: run_matching_for_listing() still checks status internally
    via the SQL function, so this can't accidentally notify tenants
    about an unverified listing.
    """
    require_supabase()
    result = run_matching_for_listing(payload.listing_id)
    return result


# ============================================
# FRAUD / TRUST SCORING ENDPOINTS
# ============================================

class ReportListingRequest(BaseModel):
    listing_id: str
    reporter_phone: str
    reason: str  # wrong_price | fake_listing | unresponsive | bait_and_switch | scam_attempt | other
    details: Optional[str] = None


VALID_REPORT_REASONS = {
    "wrong_price", "fake_listing", "unresponsive",
    "bait_and_switch", "scam_attempt", "other",
}


@app.post("/listings/report")
def report_listing(payload: ReportListingRequest):
    """
    A tenant flags a problem with a listing — wrong price in
    person, landlord unresponsive, asked for money before a
    viewing, etc. This feeds directly into the fraud score
    alongside automated signals (price drift, response spikes,
    suspicious edit timing).
    """
    require_supabase()

    if payload.reason not in VALID_REPORT_REASONS:
        raise HTTPException(
            status_code=400,
            detail=f"reason must be one of: {', '.join(sorted(VALID_REPORT_REASONS))}",
        )

    result = supabase.table("listing_reports").insert({
        "listing_id": payload.listing_id,
        "reporter_phone": payload.reporter_phone,
        "reason": payload.reason,
        "details": payload.details,
    }).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to record report.")

    # A report should immediately trigger a fraud score recompute —
    # don't wait for the next scheduled check if a tenant just
    # flagged something serious.
    fraud_result = recompute_fraud_score(payload.listing_id, trigger="report")

    return {"success": True, "report": result.data[0], "fraud_check": fraud_result}


def recompute_fraud_score(listing_id: str, trigger: str = "manual") -> dict:
    """
    The core fraud scoring trigger. Gathers all four signal inputs
    for one listing, runs calculate_fraud_score(), logs the result,
    and — if the score crosses the revert threshold — actually
    flips the listing back to 'needs_info' for re-verification,
    going through the same state machine enforcement as a normal
    agent decision.
    """
    if supabase is None:
        return {"error": "Supabase not configured."}

    listing_result = (
        supabase.table("listings")
        .select(
            "id, price_ugx, price_at_verification, area, verification_status, "
            "last_updated_at, verified_at_snapshot, landlord_id, "
            "landlords(avg_response_minutes)"
        )
        .eq("id", listing_id)
        .single()
        .execute()
    )
    if not listing_result.data:
        return {"error": "Listing not found."}
    listing = listing_result.data

    # Only score listings that have actually been verified at some point —
    # a 'pending' listing has no verification snapshot to compare against.
    if listing["verification_status"] not in ("verified", "needs_info", "in_review"):
        return {"score": 0, "recommended_action": "none", "skipped": True}

    area_avg_result = (
        supabase.table("area_avg_price").select("avg_price_ugx").eq("area", listing["area"]).single().execute()
    )
    area_avg = area_avg_result.data["avg_price_ugx"] if area_avg_result.data else None

    risk_result = supabase.table("fraud_risk_summary").select("*").eq("id", listing_id).single().execute()
    risk = risk_result.data or {}

    landlord = listing.get("landlords") or {}

    from datetime import datetime as dt

    def parse_ts(s):
        return dt.fromisoformat(s.replace("Z", "+00:00")) if isinstance(s, str) else s

    signals = {
        "price_ugx": listing["price_ugx"],
        "price_at_verification": listing.get("price_at_verification"),
        "area_avg_price": area_avg,
        "current_avg_response_minutes": landlord.get("avg_response_minutes"),
        # Real snapshot of the landlord's response time taken at the
        # moment THIS listing was verified — not the landlord's current
        # value. Comparing current-vs-current always reads "no change",
        # which silently disabled the response-spike signal entirely.
        # Comes from fraud_risk_summary, which exposes the
        # response_minutes_at_verification column set by the
        # snapshot_on_verify() database trigger.
        "response_at_verification": risk.get("response_minutes_at_verification"),
        "last_edited_at": parse_ts(listing.get("last_updated_at")),
        "verified_at": parse_ts(listing.get("verified_at_snapshot")),
        "total_reports": risk.get("total_reports", 0),
        "reports_last_7_days": risk.get("reports_last_7_days", 0),
        "report_reasons": risk.get("report_reasons") or [],
    }

    result = calculate_fraud_score(signals)

    # Update the listing's stored fraud score
    supabase.table("listings").update({
        "fraud_score": result["score"],
        "fraud_flagged": result["score"] >= FRAUD_THRESHOLD_REVIEW,
    }).eq("id", listing_id).execute()

    action_taken = "none"

    # If the score crosses the revert threshold AND the listing is
    # currently verified, push it back through the state machine —
    # exactly the decision you made: automatic reversion, not just
    # a flag for a human to eventually notice.
    if (
        result["recommended_action"] == "revert_to_needs_info"
        and listing["verification_status"] == "verified"
    ):
        if can_transition("verified", "needs_info"):
            supabase.table("listings").update({
                "verification_status": "needs_info",
                "is_verified": False,
            }).eq("id", listing_id).execute()

            supabase.table("verification_log").insert({
                "listing_id": listing_id,
                "from_status": "verified",
                "to_status": "needs_info",
                "notes": f"Auto-reverted by fraud scoring — score {result['score']} (trigger: {trigger})",
            }).execute()
            action_taken = "status_changed"
    elif result["score"] >= FRAUD_THRESHOLD_REVIEW:
        action_taken = "flagged"

    # Log this fraud check regardless of outcome — builds the
    # history of how a listing's risk evolved over time.
    supabase.table("fraud_score_log").insert({
        "listing_id": listing_id,
        "score": result["score"],
        "trigger": trigger,
        "action_taken": action_taken,
        "breakdown": result["breakdown"],
    }).execute()

    return {**result, "action_taken": action_taken}


class RecomputeFraudRequest(BaseModel):
    listing_id: str


@app.post("/admin/recompute-fraud-score")
def recompute_fraud_score_endpoint(payload: RecomputeFraudRequest):
    """
    Manually trigger a fraud score recompute for one listing —
    useful for testing, or for re-checking after a price edit
    that wasn't caught by an automated trigger yet.
    """
    require_supabase()
    return recompute_fraud_score(payload.listing_id, trigger="manual")


@app.get("/admin/fraud-queue")
def get_fraud_queue(min_score: float = Query(FRAUD_THRESHOLD_REVIEW)):
    """
    Returns every listing currently flagged at or above the
    review threshold — the queue a human moderator works through.
    """
    require_supabase()
    result = (
        supabase.table("fraud_risk_summary")
        .select("*")
        .gte("fraud_score", min_score)
        .order("fraud_score", desc=True)
        .execute()
    )
    return {"results": result.data or [], "count": len(result.data or [])}


# ============================================
# AUTH / PROFILES ENDPOINTS
# ============================================
# Phone+password login, implemented on top of Supabase Auth's
# email/password system via a deterministic phone -> pseudo-email
# mapping (see app/auth.py). The pseudo-email is an internal
# implementation detail — landlords and tenants only ever see
# and use their phone number.
 
class LandlordSignupRequest(BaseModel):
    phone: str
    password: str
    name: str
    email: Optional[str] = None  # real email, optional — different from the pseudo-email
 
 
@app.post("/auth/landlord/signup")
def landlord_signup(payload: LandlordSignupRequest):
    """
    A landlord self-registers. Creates a Supabase Auth user under
    the phone's pseudo-email, then creates the matching row in
    the landlords table, linked via auth_user_id.
    """
    require_supabase()
 
    validation = validate_signup_input(payload.phone, payload.password, payload.name)
    if not validation["valid"]:
        raise HTTPException(status_code=400, detail=validation["error"])
 
    normalized_phone = validation["normalized_phone"]
    pseudo_email = phone_to_pseudo_email(normalized_phone)
 
    # Check for an existing landlord with this phone first — a clear
    # 409 is a much better experience than a confusing Supabase
    # Auth error surfacing from deep inside the sign-up call.
    existing = (
        supabase.table("landlords").select("id").eq("phone", normalized_phone).execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="An account with this phone number already exists.")
 
    try:
        auth_result = supabase.auth.sign_up({
            "email": pseudo_email,
            "password": payload.password,
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not create account: {e}")
 
    if not auth_result.user:
        raise HTTPException(status_code=500, detail="Account creation failed.")
 
    landlord_result = supabase.table("landlords").insert({
        "auth_user_id": auth_result.user.id,
        "phone": normalized_phone,
        "name": payload.name,
        "email": payload.email,
        "created_via": "self_signup",
    }).execute()
 
    if not landlord_result.data:
        raise HTTPException(status_code=500, detail="Account created but profile setup failed. Contact support.")
 
    return {
        "success": True,
        "landlord": landlord_result.data[0],
        "session": {
            "access_token": auth_result.session.access_token if auth_result.session else None,
            "refresh_token": auth_result.session.refresh_token if auth_result.session else None,
        },
    }
 
 
class LoginRequest(BaseModel):
    phone: str
    password: str
 
 
@app.post("/auth/landlord/login")
def landlord_login(payload: LoginRequest):
    """
    A landlord logs in with phone + password. Reconstructs the
    same deterministic pseudo-email from the phone number and
    authenticates against Supabase Auth.
    """
    require_supabase()
 
    try:
        normalized_phone = normalize_phone(payload.phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
 
    pseudo_email = phone_to_pseudo_email(normalized_phone)
 
    try:
        auth_result = supabase.auth.sign_in_with_password({
            "email": pseudo_email,
            "password": payload.password,
        })
    except Exception:
        # Deliberately vague — never reveal whether the phone exists
        # but the password was wrong vs. the phone doesn't exist at
        # all. That distinction is exactly what lets an attacker
        # enumerate which phone numbers have accounts.
        raise HTTPException(status_code=401, detail="Incorrect phone number or password.")
 
    landlord_result = (
        supabase.table("landlords").select("*").eq("auth_user_id", auth_result.user.id).single().execute()
    )
 
    return {
        "success": True,
        "landlord": landlord_result.data,
        "session": {
            "access_token": auth_result.session.access_token,
            "refresh_token": auth_result.session.refresh_token,
        },
    }
 
 
class TenantSignupRequest(BaseModel):
    phone: str
    password: str
    name: str
    email: Optional[str] = None
 
 
@app.post("/auth/tenant/signup")
def tenant_signup(payload: TenantSignupRequest):
    """
    A tenant self-registers, same pattern as landlord signup.
    Having a real tenant identity (rather than just a phone number
    on each saved search) means a tenant's saved searches, their
    report history, and their notification history all link to
    ONE identity going forward.
    """
    require_supabase()
 
    validation = validate_signup_input(payload.phone, payload.password, payload.name)
    if not validation["valid"]:
        raise HTTPException(status_code=400, detail=validation["error"])
 
    normalized_phone = validation["normalized_phone"]
    pseudo_email = phone_to_pseudo_email(normalized_phone)
 
    existing = supabase.table("tenants").select("id").eq("phone", normalized_phone).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail="An account with this phone number already exists.")
 
    try:
        auth_result = supabase.auth.sign_up({
            "email": pseudo_email,
            "password": payload.password,
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not create account: {e}")
 
    if not auth_result.user:
        raise HTTPException(status_code=500, detail="Account creation failed.")
 
    tenant_result = supabase.table("tenants").insert({
        "auth_user_id": auth_result.user.id,
        "phone": normalized_phone,
        "name": payload.name,
        "email": payload.email,
    }).execute()
 
    if not tenant_result.data:
        raise HTTPException(status_code=500, detail="Account created but profile setup failed. Contact support.")
 
    return {
        "success": True,
        "tenant": tenant_result.data[0],
        "session": {
            "access_token": auth_result.session.access_token if auth_result.session else None,
            "refresh_token": auth_result.session.refresh_token if auth_result.session else None,
        },
    }
 
 
@app.post("/auth/tenant/login")
def tenant_login(payload: LoginRequest):
    """A tenant logs in with phone + password. Mirrors landlord login exactly."""
    require_supabase()
 
    try:
        normalized_phone = normalize_phone(payload.phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
 
    pseudo_email = phone_to_pseudo_email(normalized_phone)
 
    try:
        auth_result = supabase.auth.sign_in_with_password({
            "email": pseudo_email,
            "password": payload.password,
        })
    except Exception:
        raise HTTPException(status_code=401, detail="Incorrect phone number or password.")
 
    tenant_result = (
        supabase.table("tenants").select("*").eq("auth_user_id", auth_result.user.id).single().execute()
    )
 
    return {
        "success": True,
        "tenant": tenant_result.data,
        "session": {
            "access_token": auth_result.session.access_token,
            "refresh_token": auth_result.session.refresh_token,
        },
    }
 
 
def get_current_landlord(authorization: Optional[str] = Header(None)) -> dict:
    """
    FastAPI dependency — verifies a Bearer token and returns the
    calling landlord's profile. Use this on any endpoint that
    should only be callable by a logged-in landlord, instead of
    trusting a landlord_id passed in the request body (which
    anyone could fake). Usage:
 
        @app.post("/listings/my-listings")
        def my_listings(landlord: dict = Depends(get_current_landlord)):
            ...
    """
    require_supabase()
 
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")
 
    token = authorization.replace("Bearer ", "")
 
    try:
        user_result = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")
 
    if not user_result or not user_result.user:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")
 
    landlord_result = (
        supabase.table("landlords").select("*").eq("auth_user_id", user_result.user.id).single().execute()
    )
    if not landlord_result.data:
        raise HTTPException(status_code=404, detail="No landlord profile found for this account.")
 
    return landlord_result.data
 
 
@app.get("/auth/me")
def get_my_profile(landlord: dict = Depends(get_current_landlord)):
    """
    Returns the logged-in landlord's own profile. Demonstrates
    the get_current_landlord() dependency in action — the caller
    never passes their own ID, it's derived entirely from their
    auth token, so there's no way to query someone else's profile
    by guessing an ID.
    """
    return {"landlord": landlord}
 