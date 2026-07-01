import pytest

from app.auth import (
    normalize_phone,
    is_valid_uganda_phone,
    phone_to_pseudo_email,
    validate_password,
    validate_signup_input,
    MIN_PASSWORD_LENGTH,
)


def test_normalize_phone_already_canonical():
    assert normalize_phone("+256701234567") == "+256701234567"

def test_normalize_phone_with_leading_zero():
    assert normalize_phone("0701234567") == "+256701234567" 

def test_normalize_phone_without_plus():
    assert normalize_phone("256701234567") == "+256701234567"

def test_normalize_phone_nine_digits_no_prefix():
    assert normalize_phone("701234567") == "+256701234567" 

def test_normalize_phone_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_phone("not a phone number")    
    with pytest.raises(ValueError):
        normalize_phone("12345") #too short

   
def test_is_valid_uganda_phone():
    assert is_valid_uganda_phone("+256701234567") is True
    assert is_valid_uganda_phone("0701234567") is False # Not normalized
    assert is_valid_uganda_phone("+256123") is False # too short

def test_phone_to_pseudo_email():
    assert phone_to_pseudo_email("+256701234567") == "256701234567@nyumba.local" 

def test_phone_to_pseudo_email_rejects_unnormalized():
    with pytest.raises(ValueError):
        phone_to_pseudo_email("0701234567") # must normalize first


def test_phone_to_pseudo_email_is_deterministic():
    # Same phone always produces the same pseudo-email - this is
    # what makes "login" work: we reconstruct the same pseudo-email
    # from the phone number every time rather than looking anything up.
    e1 = phone_to_pseudo_email("+256701234567")
    e2 = phone_to_pseudo_email("+256701234567")
    assert e1 == e2

def  test_phone_to_pseudo_email_different_phone_differ():
    e1 = phone_to_pseudo_email("+256701234567")    
    e2 = phone_to_pseudo_email("+256709999999")
    assert e1 != e2

def test_validate_password_too_short():
    valid, error = validate_password("short")
    assert valid is False
    assert str(MIN_PASSWORD_LENGTH) in error

def test_validate_password_ok():
    valid, error = validate_password("longenoughpassword")
    assert valid is True
    assert error == ""

def test_validate_password_exact_minimum_length():
    valid, _ = validate_password("a" * MIN_PASSWORD_LENGTH) 
    assert valid is True
    valid, _ = validate_password("a" *(MIN_PASSWORD_LENGTH - 1))  
    assert valid is False

def test_validate_signup_input_all_valid():
    result = validate_signup_input("0701234567", "securepassword", "Sarah Nakamya")
    assert result["valid"] is True
    assert result["normalized_phone"] == "+256701234567"

def test_validate_signup_input_missing_name():
    result = validate_signup_input("0701234567", "securepassword", "")
    assert result["valid"] is False
    assert "name" in result["error"].lower()

def test_validate_signup_input_bad_phone():
    result = validate_signup_input("123", "securepassword", "Sarah")
    assert result["valid"] is False


def test_validate_signup_input_weak_password():
    result = validate_signup_input("0701234567", "weak", "Sarah")
    assert result["valid"] is False
    assert "password" in result["error"].lower()

def test_validate_signup_input_checks_phone_before_failing_fast_on_name():
     # Order matters for good UX: a blank name should be caught even
    # if the phone is also bad, so the user doesn't fix one field
    # only to hit a second unrelated error immediately after.
    result = validate_signup_input("not-a-phone", "securepassword", "")
    assert result["valid"] is False
    assert "name" in result["error"].lower ()




    
    


