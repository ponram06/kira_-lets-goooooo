"""
interaction_guard.py
====================
Inspection and policy enforcement for browser typing and clicking actions.

Security Rules:
1. NEVER type into password, one-time code (OTP), or payment fields (credit card, CVV, expiry).
   Detection stops immediately and instructs the user to complete it manually.
2. NEVER type secrets matching patterns in secrets_guard.known_secrets().
3. Typing strictly uses Playwright API (locator.fill) and NEVER presses Enter or submits.
4. OS-level typing (pyautogui / keybd_event) is prohibited for browser tasks.
5. In Restricted mode, ANY click on a button, submit input, or element inside a <form>
   asks confirmation first, reading back the domain (in punycode) and the exact button text.
6. The keyword list (submit, buy, post, delete, log in, etc.) acts as a backstop.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from backend import secrets_guard
from backend.browser.url_guard import to_punycode
from backend.policy import Mode

logger = logging.getLogger(__name__)

# Keywords identifying password, OTP, 2FA, or verification code fields
CREDENTIAL_KEYWORDS = (
    "password",
    "passwd",
    "pwd",
    "passcode",
    "otp",
    "2fa",
    "mfa",
    "one-time",
    "onetime",
    "token",
    "verification code",
    "security code",
    "pin",
)

# Keywords identifying credit card, banking, and payment fields
PAYMENT_KEYWORDS = (
    "card",
    "credit",
    "debit",
    "cvv",
    "cvc",
    "ccv",
    "cid",
    "security code",
    "expir",
    "expiration",
    "cardholder",
    "billing",
    "iban",
    "routing",
    "bank",
    "account number",
)

# Autocomplete attribute values that must trigger refusal
SENSITIVE_AUTOCOMPLETE = (
    "current-password",
    "new-password",
    "one-time-code",
    "cc-number",
    "cc-csc",
    "cc-exp",
    "cc-exp-month",
    "cc-exp-year",
    "cc-type",
    "cc-name",
)

# High-risk click keywords (used as backstop in Free mode, and for role classification)
HIGH_RISK_CLICK_KEYWORDS = (
    "submit",
    "buy",
    "purchase",
    "order",
    "place order",
    "checkout",
    "pay",
    "post",
    "tweet",
    "publish",
    "send",
    "transfer",
    "delete",
    "remove",
    "log in",
    "login",
    "sign in",
    "signin",
    "sign up",
    "signup",
    "register",
    "confirm",
)

# JS script to inspect field sensitivity before typing
INSPECT_INPUT_SENSITIVITY_JS = """
(element) => {
    if (!element) return { sensitive: false, reason: '' };

    const tag = (element.tagName || '').toLowerCase();
    const type = (element.getAttribute('type') || '').toLowerCase();
    const name = (element.getAttribute('name') || '').toLowerCase();
    const id = (element.getAttribute('id') || '').toLowerCase();
    const placeholder = (element.getAttribute('placeholder') || '').toLowerCase();
    const ariaLabel = (element.getAttribute('aria-label') || '').toLowerCase();
    const autocomplete = (element.getAttribute('autocomplete') || '').toLowerCase();

    // Check associated label text if present
    let labelText = '';
    if (element.labels && element.labels.length > 0) {
        labelText = Array.from(element.labels).map(l => l.innerText).join(' ').toLowerCase();
    }

    return {
        tag: tag,
        type: type,
        name: name,
        id: id,
        placeholder: placeholder,
        ariaLabel: ariaLabel,
        autocomplete: autocomplete,
        labelText: labelText
    };
}
"""

# JS script to inspect element for click risk (form membership, button role, button text)
INSPECT_CLICK_ELEMENT_JS = """
(element) => {
    if (!element) return { isButton: false, inForm: false, text: '', role: '' };

    const tag = (element.tagName || '').toLowerCase();
    const type = (element.getAttribute('type') || '').toLowerCase();
    const role = (element.getAttribute('role') || '').toLowerCase();
    const ariaLabel = (element.getAttribute('aria-label') || '').trim();
    const innerText = (element.innerText || '').trim();
    const value = (element.getAttribute('value') || '').trim();

    const isButton = (
        tag === 'button' ||
        role === 'button' ||
        (tag === 'input' && ['submit', 'button', 'image'].includes(type))
    );

    // Check if element or any ancestor is a <form>
    const inForm = !!element.closest('form');

    const buttonText = innerText || ariaLabel || value || (element.getAttribute('title') || '').trim() || tag;

    return {
        tag: tag,
        type: type,
        role: role,
        isButton: isButton,
        inForm: inForm,
        text: buttonText
    };
}
"""


def is_sensitive_field(meta: dict[str, str], field_label: str = "") -> tuple[bool, str]:
    """
    Examine element metadata and label for passwords, OTPs, or payment fields.
    Returns (is_sensitive, reason).
    """
    # 1. Type is password
    field_type = meta.get("type", "").lower()
    if field_type == "password":
        return True, "detected password field"

    combined = " ".join([
        field_label.lower(),
        meta.get("name", ""),
        meta.get("id", ""),
        meta.get("placeholder", ""),
        meta.get("ariaLabel", ""),
        meta.get("labelText", ""),
    ])

    autocomplete = meta.get("autocomplete", "").lower()
    for sa in SENSITIVE_AUTOCOMPLETE:
        if sa in autocomplete:
            return True, f"detected sensitive autocomplete '{sa}'"

    for kw in CREDENTIAL_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", combined):
            return True, f"detected credential/OTP field ('{kw}')"

    for kw in PAYMENT_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", combined):
            return True, f"detected payment/financial field ('{kw}')"

    return False, ""


def check_typing_allowed(
    field_meta: dict[str, str],
    field_label: str,
    text_to_type: str,
) -> tuple[bool, str]:
    """
    Validate that typing into field_label with text_to_type is permissible.
    Refuses passwords, OTPs, payment data, and sensitive secrets from .env.
    """
    # 1. Check field sensitivity
    sensitive, reason = is_sensitive_field(field_meta, field_label)
    if sensitive:
        return (
            False,
            f"Action blocked: Refused to type into sensitive field '{field_label}' ({reason}). "
            "Please enter your credentials or payment details manually.",
        )

    # 2. Check text against secrets from configuration (.env / contacts)
    if secrets_guard.contains_secret(text_to_type):
        return (
            False,
            "Action blocked: Text to type contains sensitive secrets from configuration.",
        )

    return True, ""


def check_click_confirmation_needed(
    meta: dict[str, Any],
    domain: str,
    mode: Mode,
) -> tuple[bool, str]:
    """
    Determine if clicking element requires user confirmation.
    
    Rules:
    - In Restricted mode: ANY click on a button, submit input, or element inside a <form> asks first.
    - In Free mode: High-risk keywords (submit, buy, delete, log in, etc.) ask first as backstop.
    
    Returns (needs_confirm, prompt_text).
    """
    punycode_domain = to_punycode(domain)
    button_text = meta.get("text", "").strip() or meta.get("tag", "element")
    # Clean text to single line
    clean_button_text = " ".join(button_text.split())[:50]

    is_button = meta.get("isButton", False)
    in_form = meta.get("inForm", False)
    role = meta.get("role", "").lower()

    # Keyword check
    btn_text_lower = clean_button_text.lower()
    matches_high_risk = any(kw in btn_text_lower for kw in HIGH_RISK_CLICK_KEYWORDS)

    # Restricted mode: any button, submit input, or form element requires confirmation
    if mode == Mode.RESTRICTED:
        if is_button or in_form or matches_high_risk or role in ("button", "link"):
            prompt = (
                f"Confirmation required: Kira would like to click '{clean_button_text}' "
                f"on '{punycode_domain}'. Allow?"
            )
            return True, prompt

    # Free mode: high-risk actions (submit, buy, post, delete, log in) still require confirmation
    if matches_high_risk:
        prompt = (
            f"Confirmation required: Kira would like to click '{clean_button_text}' "
            f"on '{punycode_domain}'. Allow?"
        )
        return True, prompt

    return False, ""
