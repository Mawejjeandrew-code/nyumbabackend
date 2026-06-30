from datetime import datetime, timezone, timedelta
from typing import Optional, TypedDict

FRAUD_THRESHOLD_REVIEW = 40 # score >= 40: flag for human attention
FRAUD_THRESHOLD_HIDE = 70 # Score >= 70: flip back to need_info

#How much the price drift from verification snapshot
#Before it counts as suspicious (20% = meaningful change,
# not just rounding or small landlord adjustment)

SUSPICIOUS_PRICE_DROP_PCT = 0.20

# How mat times slower a landlod's response must get
# before it counts as a spikr (3x = genuinely unusal)
RESPONSE_SPIKE_MULTIPLIER = 3.0

#How quickly after verification a heavy is suspicious
# (edits within 48 hours of getting the badge are a red flag)
EDIT_AFTER_VERIFY_HOURS = 48

class ListingSignals(TypedDict, total=False):
    """All the raw facts the fraud scorer needs about one listing."""
    # Price signals
    price_ugx: int
    price_at_verification: Optional[int] # Price when badge was awarded
    area_avg_price: Optional[float]

    # Response time signals
    current_avg_response_minutes: Optional[float]
    response_at_verifiction: Optional[float] #Baseline when verified


    # Edit pattern signals
    last_edited_at: Optional[datetime]
    verified_at: Optional[datetime]

    # Tenant reports
    total_reports: int
    reports_last_7_days: int
    report_reasons:list[str]

 # Individual signal scorers
# Each return 0-100 where 100 = maximally suspicious
#Deliberately the inverse of ranking.py's scorers.

def score_price_drift(
        current_price: int,
        price_at_verification: Optional[int],
        area_avg_price: Optional[float],
) -> float:
    """
    How much has the price changed since verification?
    A drop of 20%+ after getting the badge is suspicious —
    the classic "get verified at a fair price, then lower
    it to bait-and-switch territory." Measures both drift
    from the verified snapshot AND from the area average
    (same anti-scam logic as ranking.py, but inverted).
 
    Uses the FULL 0-100 range so that combined with other
    maxed-out signals, an extreme case can actually reach
    the revert-to-needs_info threshold.

    """
    score = 0.0

    # Signal 1: price dropped significantly sice verification
    if price_at_verification and price_at_verification > 0:
        drop_ratio = (price_at_verification - current_price) / price_at_verification

        if drop_ratio >= SUSPICIOUS_PRICE_DROP_PCT:
            #Scale from 0 at exactly 20% drop to 100 at 70%+ drop
            score = min(100.0, (drop_ratio - SUSPICIOUS_PRICE_DROP_PCT) / 0.5 * 100)

    # sIGNAL 2: PRICE IS NOW SUSPICIOUSLY BELOW AREA average
    if area_avg_price and area_avg_price > 0:
        ratio = current_price / area_avg_price
        if ratio < 0.4:
            score = max(score, 75.0) #Suspiciously cheap regardless of drift

    return round(score, 1)        

def score_response_spike(
        current_avg_minutes: Optional[float],
        baseline_minutes: Optional[float],
) -> float:
    """
      Has the landlord's response time gotten significantly
    worse since they were verified? A landlord who replied
    in 30 min at verification but now averages 12 hours
    may have abandoned the listing, rented it elsewhere,
    or is actively avoiding inquiries.
 
    Uses the full 0-100 range — same reasoning as price_drift,
    so combined extreme signals can reach the action thresholds.
    

    """
    if current_avg_minutes is None or baseline_minutes is None:
        return 0.0 # no data - don't penalise
    
    if baseline_minutes == 0:
        baseline_minutes = 1.0 #Avoid division by zero

    spike_ratio = current_avg_minutes /baseline_minutes
    if spike_ratio < RESPONSE_SPIKE_MULTIPLIER:
        return 0.0

    # Scale from 0 at exactly 3x to 100 at 15x+ spike
    score = min(100.0, (spike_ratio - RESPONSE_SPIKE_MULTIPLIER) / 12.0 * 100)
    return round(score, 1)    

def score_edit_after_verification(
        last_editied_at: Optional[datetime],
        verified_at: Optional[datetime],
) -> float:
    """
    Was the listing edited heavily within 48 hours of
    getting the verified badge? That's the bait-and-switch
    pattern: get the badge with legitimate details, then
    quietly change them.
 
    Uses the full 0-100 range — an edit in the first hour
    after verification is maximally suspicious.
    """
    if last_editied_at is None or verified_at is None:
        return 0.0
    
    if last_editied_at.tzinfo is None:
        last_editied_at = last_editied_at.replace(tzinfo=timezone.utc)

    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=timezone.utc)    
        
    hours_after_verify = (last_editied_at - verified_at).total_seconds() / 3600
    if hours_after_verify < 0:
        return 0.0 #edited before verification - fine

    if hours_after_verify <= EDIT_AFTER_VERIFY_HOURS:
        #quick edit right after verification - suspicious.
        # Score highest (100) for an edit in the first moments,
        # decaying to 0 at the 48-hour boundary.
        score = 100.0 * (1 - hours_after_verify / EDIT_AFTER_VERIFY_HOURS) 
        return round(score, 1)  
    return 0.0

def score_tenant_reports(
        total_reports: int,
        reports_last_7_days: int,
        report_reasons: list[str],
        
) -> float:
    """
    Tenant reports are the most direct signal — real people
    who actually tried to engage with this listing and found
    something wrong. Scores based on volume, recency, and
    whether severe reason types appear (scam_attempt is worse
    than unresponsive).
    """
    if total_reports == 0:
        return 0.0
    
    # Base score: number of reports (caps at 60)
    base = min(60.0, total_reports * 12)

    # Recovery bonus: recent reports are more significant
    recency_boost = min(20.0, reports_last_7_days * 10)

    # Severity boost: scam_attempt and fake_listing are worst
    severe_reasons = {"scam_attempt", "fake_listing", "bait_and_switch"}
    has_severe = bool(set(report_reasons or []) & severe_reasons)
    severity_boost = 20.0 if has_severe else 0.0

    score = base + recency_boost + severity_boost
    return round(min(100.0, score), 1)

 
# WEIGHTS — how much each signal contributes

# Tenant reports carry the most weight — a real human
# reporting a problem is a stronger signal than any
# automated pattern. Price drift is next, since it's
# a concrete, measurable change to a core listing fact.
DEFAULT_FRAUD_WEIGHTS = {
    "price_drift":     0.25,
    "response_spike":  0.15,
    "edit_pattern":    0.20,
    "tenant_reports":  0.40,
}

#MAIN SCORING FUNCTION

def calculate_fraud_score(
    signals: ListingSignals,
    weights: dict = None,

) -> dict:
      
      """
        Computes a 0-100 fraud risk score for one listing.
        0 = completely clean. 100 = pull it immediately.
 
        Returns score + breakdown + recommended_action so the
        caller knows exactly what to do without having to
        re-implement the threshold logic.
        """
      weights = weights or DEFAULT_FRAUD_WEIGHTS
      total_weight = sum(weights.values())
      norm = {k: v / total_weight for k, v in weights.items()}

      raw_signals = {
          "price_drift": score_price_drift(
              signals.get("price_ugx", 0),
              signals.get("price_at_verification="),
              signals.get("area_avg_price"),
          ),
          "response_spike": score_response_spike(
              signals.get("current_avg_response_minutes"),
              signals.get("response_at_verifiction"),
          ),
          "edit_pattern": score_edit_after_verification(
              signals.get("last_edited_at"),
              signals.get("verified_at"),
          ),
          "tenant_reports": score_tenant_reports(
              signals.get("total_reports",  0),
              signals.get("reports_last_7_days", 0),
              signals.get("report_reasons") or [],
            ),

        }
      score = sum(raw_signals[k] * norm[k] for k in raw_signals)
      score = round(min(100.0, score), 1)

      breakdown = {
          k: {
              "raw": raw_signals[k],
              "weight": round(norm[k], 4),
              "contribution": round(raw_signals[k] * norm[k], 1),
          }
          for k in raw_signals
      }

      # Recommended action based on score thresholds
      if score >= FRAUD_THRESHOLD_HIDE:
          action = "revert_to_needs_info"
      elif score >= FRAUD_THRESHOLD_REVIEW:
          action= "flag_for_review"
      else:
          action = "none"       

      return {
          "score": score,
          "breakdown": breakdown,
          "recommended_action": action,
          "thresholds": {
              "flag_at": FRAUD_THRESHOLD_REVIEW,
              "revert_at": FRAUD_THRESHOLD_HIDE,
          },

      }   
      

