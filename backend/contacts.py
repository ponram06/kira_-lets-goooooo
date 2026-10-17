"""
contacts.py
===========
Secure contact lookup, E.164 phone number validation, and phone number masking.

Storage:
- %APPDATA%\\Kira\\contacts.yaml (or %KIRA_USER_DIR%\\contacts.yaml), outside git.
- Read-only: Kira cannot add, modify, or list contacts.
- Model input guard: The model must provide a contact name; raw numbers are rejected.
- Ambiguity safety: Returns at most 3 candidate names; never sends the full contact list.
- Masking: All but the last 3 digits are masked in confirm prompts, logs, and events.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from backend.user_config import user_dir

logger = logging.getLogger(__name__)

CONTACTS_FILENAME = "contacts.yaml"

# E.164 validation regex: optional leading '+', 8 to 15 digits total
_E164_PATTERN = re.compile(r"^\+?[1-9]\d{7,14}$")

# Detection regex for model passing raw phone number strings
_PHONE_DETECTION = re.compile(r"^\+?[\d\s\-()]{6,}$")


@dataclass(frozen=True)
class ContactResolveResult:
    status: str  # "found", "ambiguous", "not_found", "model_number_rejected"
    contact_name: str
    phone: str
    masked_phone: str
    candidates: list[str]  # at most 3 candidate names
    message: str


def contacts_path() -> Path:
    """Return the path to contacts.yaml in the user config directory outside git."""
    return user_dir(create=False) / CONTACTS_FILENAME


def mask_phone(phone: str) -> str:
    """
    Mask all but the last 3 digits of a phone number.
    e.g. '+15551234567' -> '+*****567'
         '15551234567'  -> '*****567'
    """
    if not phone:
        return ""
    clean = phone.strip()
    prefix = "+" if clean.startswith("+") else ""
    digits = re.sub(r"\D", "", clean)
    if len(digits) <= 3:
        return prefix + digits
    masked = "*" * (len(digits) - 3) + digits[-3:]
    return prefix + masked


def validate_e164_phone(phone: str) -> tuple[bool, str]:
    """
    Validate that *phone* adheres to E.164 standards with country code.
    Returns (is_valid, cleaned_digits_with_plus).
    """
    if not phone or not isinstance(phone, str):
        return False, ""
    clean = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not _E164_PATTERN.match(clean):
        return False, ""
    # Ensure standard +prefix format
    if not clean.startswith("+"):
        clean = "+" + clean
    return True, clean


def is_raw_phone_number(query: str) -> bool:
    """Check if the model passed a phone-like string instead of a contact name."""
    if not query or not isinstance(query, str):
        return False
    q = query.strip()
    digits = re.sub(r"\D", "", q)
    return len(digits) >= 6 or bool(_PHONE_DETECTION.match(q))


def load_contacts() -> dict[str, str]:
    """
    Load contacts from contacts.yaml.
    Returns dict mapping lowercase contact name -> clean E.164 phone string.
    """
    path = contacts_path()
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        raw_contacts = data.get("contacts") if isinstance(data, dict) else {}
        if not isinstance(raw_contacts, dict):
            return {}

        validated: dict[str, str] = {}
        for name, num in raw_contacts.items():
            if not isinstance(name, str) or not num:
                continue
            is_valid, clean_num = validate_e164_phone(str(num))
            if is_valid:
                validated[name.strip().lower()] = clean_num
            else:
                logger.warning("Invalid phone number in contacts.yaml for '%s'", name)
        return validated
    except Exception as exc:
        logger.warning("Failed reading contacts.yaml (%s)", exc)
        return {}


def resolve_contact(query: str) -> ContactResolveResult:
    """
    Resolve a contact name case-insensitively with fuzzy matching.
    Strictly refuses raw phone numbers supplied by the model.
    Never returns more than 3 candidate names.
    """
    clean_q = (query or "").strip()
    if not clean_q:
        return ContactResolveResult(
            status="not_found",
            contact_name="",
            phone="",
            masked_phone="",
            candidates=[],
            message="Contact name cannot be empty.",
        )

    # 1. Guard against model passing a raw phone number directly
    if is_raw_phone_number(clean_q):
        contacts = load_contacts()
        # Check if the number happens to exist in contacts
        clean_num = clean_q.replace(" ", "").replace("-", "")
        if not clean_num.startswith("+"):
            clean_num = "+" + clean_num
        matched_name = None
        for cname, cnum in contacts.items():
            if cnum == clean_num:
                matched_name = cname
                break
        if not matched_name:
            return ContactResolveResult(
                status="model_number_rejected",
                contact_name="",
                phone="",
                masked_phone="",
                candidates=[],
                message="Refused: You must specify a contact name from contacts.yaml, not a phone number.",
            )

    contacts = load_contacts()
    if not contacts:
        return ContactResolveResult(
            status="not_found",
            contact_name="",
            phone="",
            masked_phone="",
            candidates=[],
            message="No contacts found in contacts.yaml.",
        )

    norm_q = clean_q.lower()

    # 2. Exact match pass
    if norm_q in contacts:
        phone = contacts[norm_q]
        return ContactResolveResult(
            status="found",
            contact_name=clean_q,
            phone=phone,
            masked_phone=mask_phone(phone),
            candidates=[clean_q],
            message=f"Resolved contact '{clean_q}'",
        )

    # 3. Fuzzy matching
    scored: list[tuple[float, str]] = []
    for cname in contacts:
        ratio = difflib.SequenceMatcher(None, norm_q, cname).ratio()
        if norm_q in cname or cname in norm_q:
            ratio = max(ratio, 0.85)
        if ratio >= 0.40:
            scored.append((ratio, cname))

    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored:
        return ContactResolveResult(
            status="not_found",
            contact_name="",
            phone="",
            masked_phone="",
            candidates=[],
            message=f"Contact '{query}' not found in contacts.yaml.",
        )

    top_score, top_name = scored[0]
    runner_up_score = scored[1][0] if len(scored) > 1 else 0.0

    # Strong confident match
    if top_score >= 0.85 and (top_score - runner_up_score) >= 0.15:
        phone = contacts[top_name]
        return ContactResolveResult(
            status="found",
            contact_name=top_name,
            phone=phone,
            masked_phone=mask_phone(phone),
            candidates=[top_name],
            message=f"Resolved contact '{query}' to '{top_name}'",
        )

    # Ambiguous match: ask one candidate at a time with yes/no
    top_candidates = [s[1] for s in scored[:3]]
    if top_score >= 0.60 and (top_score - runner_up_score) < 0.15:
        return ContactResolveResult(
            status="ambiguous",
            contact_name=top_name,
            phone="",
            masked_phone="",
            candidates=top_candidates,
            message=f"Did you mean {top_name}? (yes/no)",
        )

    # Weak match: suggest at most 3 candidates
    candidate_str = ", ".join(f"'{c}'" for c in top_candidates)
    return ContactResolveResult(
        status="not_found",
        contact_name="",
        phone="",
        masked_phone="",
        candidates=top_candidates,
        message=f"Contact '{query}' not found. Did you mean {candidate_str}?",
    )
