#

import os
import africastalking

AT_USERNAME = os.environ.get("AT_USERNAME", "")
AT_API_KEY = os.environ.get("AT_API_KEY", "")
AT_SENDER_ID = os.environ.get("AT_SENDER_ID", "NYUMBA")

_initialized = False


def _ensure_initialized():
    global _initialized
    if not _initialized and AT_USERNAME and AT_API_KEY:
        africastalking.initialize(AT_USERNAME, AT_API_KEY)
        _initialized = True


def send_match_sms(phone: str, message: str) -> dict:
    """
    Sends one notification SMS. Returns a dict with
    success/error info — never raises, so a failed SMS
    never crashes the matching pipeline.
    """
    if not AT_USERNAME or not AT_API_KEY:
        return {"success": False, "error": "Africa's Talking not configured."}

    _ensure_initialized()
    sms = africastalking.SMS

    try:
        response = sms.send(message, [phone], sender_id=AT_SENDER_ID)
        recipients = response.get("SMSMessageData", {}).get("Recipients", [])
        if recipients and recipients[0].get("status") == "Success":
            return {"success": True, "message_id": recipients[0].get("messageId")}
        return {"success": False, "error": recipients[0].get("status") if recipients else "Unknown error"}
    except Exception as e:
        return {"success": False, "error": str(e)}