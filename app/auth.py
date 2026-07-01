import re

#Uganda phone numbers: @256 followed by 9 digits, e.g.  +256701234567
UGANDA_PHONE_PATTERN = re.compile(r"^\+256[0-9]{9}$")

PSUEDO_EMAIL_DOMAIN = "nyumba.local"

MIN_PASSWORD_LENGTH = 8

def normalize_phone(raw_phone: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw_phone.strip())

    if digits.startswith("+256") and len(digits) == 13:
        return  digits
    if digits.startswith("256") and len(digits) == 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+256" + digits[1:]
    if len(digits) == 9 and digits[0] != "0":
        return "+256" + digits
    
    raise ValueError(
        f"Could not normalize phone number '{raw_phone}'. "
        f"Expected a uganda number like 0701234567 or +256701234567."

    )
def is_valid_uganda_phone(phone: str) -> bool:
    """Checks if a phobe number is already in canonical +256xxxxxxx form."""
    return bool(UGANDA_PHONE_PATTERN.match(phone))

def phone_to_pseudo_email(phone: str) -> str:
    """
    Converts a normalized phone number into the internal pseudo-
    email Supabase Auth actually stores. This value is NEVER shown
    to the user — it exists purely so we can use Supabase's tested
    email/password auth instead of hand-rolling password storage.
    Raises ValueError if the phone isn't already normalized, to
    catch bugs early rather than silently creating malformed
    pseudo-emails.
    """
    if not is_valid_uganda_phone(phone):
        raise ValueError(
            f"Phone '{phone}' is not normalized +256xxxxxxx form. "
            f"Call normalize_phone() first."
        )
    # Strip the '+' since some validators reject it in the local part
    local_part = phone.replace("+", "")
    return f"{local_part}@{PSUEDO_EMAIL_DOMAIN}"

def validate_password(password: str) -> tuple[bool, str]:
    """
    Basic password strength check. Returns (is_valid, error_message).
    Kept deliberately simple for v1 — length only, no complexity
    rules that tend to just frustrate users without meaningfully
    improving security (modern guidance favors length over forced
    character-class mixing).
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return True, ""

def validate_signup_input(phone: str, password: str, name: str) -> dict:
    """
    Runs all signup validation in one place, returning a dict with
    either {'valid': True, 'normalized_phone': ...} or
    {'valid': False, 'error': ...}. Centralizing this means both
    the landlord and tenant signup endpoints in main.py share
    identical validation logic instead of two slightly-different
    copies drifting apart over time.
    """
    if not name or not name.strip():
        return {"valid": False, "error": "Name is required."}
    
    try:
        normalized = normalize_phone(phone)
    except ValueError as e:
        return {"valid": False, "error": str(e)} 

    is_valid_pw, pw_error = validate_password(password)  
    if not is_valid_pw:
        return {"valid": False, "error": pw_error}
    
    return {"valid": True, "normalized_phone": normalized}
     
    
