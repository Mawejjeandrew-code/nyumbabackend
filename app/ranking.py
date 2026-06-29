from datetime import datetime, timezone
from typing import Optional, TypedDict


DEFAULT_WEIGHTS = {
    "verification": 0.30,
    "response": 0.20,
    "freshness":0.20,
    "photos": 0.15,
    "price": 0.15,
}

class Listing(TypedDict, total=False):
    id: str
    is_verified: bool
    photo_count: int
    price_ugx: int
    last_updated_at: datetime
    area: Optional[str]

class Landlord(TypedDict, total=False):
    avg_response_minutes: Optional[float]

def score_verification(listing: Listing) -> float: 
    return 100.0 if listing.get("is_verified") else 0.0

def score_respose_speed(avg_response_minutes: Optional[float]) -> float:
    if avg_response_minutes is None:
        return 40.0
    HALF_LIFE_MINUTES = 180
    score = 100.0 * (0.5 **(avg_response_minutes / HALF_LIFE_MINUTES))
    return max(0.0, min(100.0, score))

def score_freshness(last_updated_at: datetime) -> float:
    now = datetime.now(timezone.utc)
    if last_updated_at.tzinfo is None:
        last_updated_at = last_updated_at.replace(tzinfo=timezone.utc)
    days_since = (now - last_updated_at).total_seconds() /86400
    DECAY_DAYS = 30
    score = 100.0 * (0.5 ** (days_since / DECAY_DAYS))
    return max(0.0, min(100.0, score))

def score_photo_completness(photo_count: int) -> float:
    TARGET_PHOTOS = 6
    score = (min(photo_count, TARGET_PHOTOS) / TARGET_PHOTOS) * 100
    return round(score)

def score_price_competitiveness(price: float, area_avg_price: Optional[float]) -> float:
    if not area_avg_price or area_avg_price <= 0:
        return 50.0
    
    ratio = price / area_avg_price

    if ratio < 0.4:
        return 20.0
    
    if 0.7 <= ratio <= 1.0:
        return 100.0 # Sweet spot
    
    if 1.0 < ratio <= 1.3:
        return round(100 - ((ratio - 1.0) / 0.3) * 40)
    
    if 0.4 <= ratio < 0.7:
        return round(60 + ((ratio - 0.4) / 0.3) * 40)
    
    #significantly over priced ( >130% of average)
    score = 60 - ((ratio - 1.3) * 50)
    return max(0.0, round(score))


#Main scoring function
def calculate_listing_score(
    listing: Listing,
    landlord: Optional[Landlord],
    area_avg_price: Optional[float],
    weignhts: dict = None,

) -> dict:
    weights = weignhts or DEFAULT_WEIGHTS
    total_weight = sum(weights.values())
    norm = {k: v / total_weight for k, v in weights.items()}

    avg_response = landlord.get("avg_response_minutes") if landlord else None
    
    signals = {
        "verification": score_verification(listing),
        "response": score_respose_speed(avg_response),
        "freshness": score_freshness(listing["last_updated_at"]),
        "photos": score_photo_completness(listing.get("photo_count", 0)),
        "price": score_price_competitiveness(listing.get("price_ugx", 0), area_avg_price),

    }

    score = sum(signals[k] * norm[k] for k in signals)

    breakdown = {
        k: {
            "raw": round(signals[k], 1),
            "weight": round(norm[k], 4),
            "contribution": round(signals[k] * norm[k], 1),

        }
        for k in signals

    }
    return {"score": round(score, 1), "breakdown": breakdown}

def rank_listings(
        listings: list[Listing],
        area_avg_prices: dict = None,
        weights: dict = None,
) -> list[dict]:
    area_avg_prices = area_avg_prices or {}
    scored = []
    for listing in listings:
        area_avg = area_avg_prices.get(listing.get("area"))
        landlord = listing.get("landlord")
        result = calculate_listing_score(listing, landlord, area_avg, weights)
        scored.append({**listing, "_score": result["score"], "_score_breakdown": result["breakdown"]})

    return sorted(scored, key=lambda l: l["_score"], reverse=True)    


