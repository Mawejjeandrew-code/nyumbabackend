# ============================================
# NYUMBA — EMAIL NOTIFICATIONS (Resend)
# File: app/email.py
# ============================================
# Setup:
#   pip install resend
#   Add to .env:
#     RESEND_API_KEY=re_xxxxxxxxxxxx
#     RESEND_FROM_EMAIL=notifications@nyumba.ug
# ============================================
# Resend requires a verified sending domain before you
# can send from a custom address like notifications@nyumba.ug.
# Until your domain is verified, Resend's sandbox lets you
# send from onboarding@resend.dev to YOUR OWN email only —
# fine for testing, not for real tenant notifications yet.
# ============================================

import os
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")


def send_match_email(to_email: str, subject: str, body: str) -> dict:
    """
    Sends one notification email. Returns a dict with
    success/error info — never raises, so a failed email
    never crashes the matching pipeline (matching SMS failure
    handling in lib/sms.js follows the same non-fatal pattern).
    """
    if not resend.api_key:
        return {"success": False, "error": "RESEND_API_KEY not configured."}

    try:
        result = resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": subject,
            "text": body,
        })
        return {"success": True, "id": result.get("id")}
    except Exception as e:
        return {"success": False, "error": str(e)}