# ============================================
# NYUMBA — RANKING SERVICE (FastAPI)
# File: app/main.py
# ============================================
# Standalone Python service handling search ranking.
# Your Next.js frontend calls this over HTTP instead
# of running the algorithm in a Next.js API route.
# ============================================
# Run locally:
#   uvicorn app.main:app --reload --port 8000
# ============================================

import os
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

from app.ranking import rank_listings, DEFAULT_WEIGHTS
from app.verification import (
    can_transition,
    can_resubmit,
    build_resubmission_update,
)

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

    return {"success": True, "new_status": payload.new_status}


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