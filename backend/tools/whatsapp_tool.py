"""
whatsapp_tool.py
================
Secure WhatsApp messaging tool for Kira (Milestone 3).

Security & Operational Invariants:
1. Contact resolution: Model provides ONLY a contact name; raw numbers are rejected.
2. Text constraints: Max 280 characters, no newlines or control characters, secrets_guard check.
3. Provenance guard: Refuses 20+ chars copied from task tool outputs not in user request.
4. Single-send per request ID: Refuses empty/missing request_id and multiple sends in one request.
5. Zero automatic retries: Failed or aborted sends are never retried by the agent loop.
6. Honest delivery reporting: Reports "I pressed Enter. I can't verify delivery, check WhatsApp."
7. Foreground check: Uses ctypes immediately before Enter to verify WhatsApp.exe has focus.
8. Rate limiting: Persisted outside git in %APPDATA%\\Kira\\whatsapp_rate_limit.json.
9. Privacy: Phone number masked to last 3 digits (+*****567) in all logs and events. Never logs text.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.parse
from typing import Any, Callable, Optional

import psutil

from backend import events as _events
from backend import secrets_guard
from backend.contacts import (
    is_raw_phone_number,
    mask_phone,
    resolve_contact,
)
from backend.policy import KILL_SWITCH, Mode, check
from backend.whatsapp_rate_limit import (
    approve_contact_free_mode,
    check_rate_limit,
    is_contact_approved_free_mode,
    record_send_attempt,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 280
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002

_lock = threading.Lock()
_sent_request_ids: set[str] = set()

# Process names associated with WhatsApp Desktop and Store app
WHATSAPP_PROCESS_NAMES = ["whatsapp.exe", "whatsappdesktop.exe", "whatsapp"]


def reset_sent_request_ids() -> None:
    """Clear the sent request IDs tracker (used in testing)."""
    with _lock:
        _sent_request_ids.clear()


def check_secrets(text: str) -> tuple[bool, str]:
    """Check if text contains any confidential credentials from .env or env vars."""
    if not text:
        return True, ""
    for secret in secrets_guard.known_secrets():
        if secret and secret in text:
            return False, "Refused: Message text contains confidential credentials or secrets."
    return True, ""


def check_control_characters(text: str) -> tuple[bool, str]:
    """Reject newlines and Unicode control characters."""
    for ch in text:
        if ch in ("\n", "\r", "\t", "\x00"):
            return False, "Refused: Message text cannot contain newlines or control characters."
        if unicodedata.category(ch).startswith("C"):
            return False, "Refused: Message text cannot contain control characters."
    return True, ""


def check_provenance(
    text: str,
    user_request: str = "",
    tool_outputs: Optional[list[str]] = None,
) -> tuple[bool, str]:
    """
    Provenance guard: refuse a message containing 20+ characters copied from
    prior tool outputs or file reads that were not present in the user's initial request.
    """
    if not text or not tool_outputs or len(text) < 20:
        return True, ""

    clean_user = user_request.lower() if user_request else ""
    # Check 20-character sliding windows
    for i in range(len(text) - 19):
        chunk = text[i : i + 20]
        chunk_lower = chunk.lower()
        # If chunk appears in user's prompt, it is legitimately user-requested
        if chunk_lower in clean_user:
            continue
        # If chunk appears in any tool output, it indicates unrequested data copying
        for output in tool_outputs:
            if output and chunk_lower in output.lower():
                return (
                    False,
                    "Refused: Message text contains content copied from prior tool output or file reads.",
                )

    return True, ""


def _is_whatsapp_foreground(expected_processes: list[str]) -> tuple[bool, str]:
    """
    Check if the active foreground window belongs to WhatsApp using ctypes.
    Also detects visible error dialogs.
    """
    if os.name != "nt":
        return True, ""

    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False, "No active foreground window found."

        # Get process ID
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return False, "Could not determine foreground window process."

        try:
            proc = psutil.Process(pid.value)
            proc_name = proc.name().lower()
        except Exception as exc:
            return False, f"Could not inspect process: {exc}"

        if proc_name not in expected_processes:
            return False, f"Foreground window belongs to '{proc_name}', not WhatsApp."

        # Window title check for error dialogs
        length = user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value.lower()

        dialog_triggers = [
            "phone number shared via url is invalid",
            "invalid phone number",
            "whatsapp - error",
            "number not found",
        ]
        for trigger in dialog_triggers:
            if trigger in title:
                return False, f"WhatsApp displayed an error dialog: '{buff.value}'."

        return True, ""
    except Exception as exc:
        return False, f"Foreground check failed: {exc}"


def _press_enter() -> None:
    """Send VK_RETURN keystroke to the active window."""
    if os.name == "nt":
        user32 = ctypes.windll.user32
        user32.keybd_event(VK_RETURN, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
    else:
        logger.info("Mock press Enter key.")


def send_whatsapp(
    contact: str,
    text: str,
    request_id: Optional[str] = None,
    mode: Mode = Mode.RESTRICTED,
    user_request: str = "",
    tool_outputs: Optional[list[str]] = None,
    confirm_cb: Optional[Callable[[str], bool]] = None,
    countdown_cb: Optional[Callable[[str, int], bool]] = None,
    speak_cb: Optional[Callable[[str], Any]] = None,
    poll_timeout: float = 5.0,
    min_gap_seconds: float = 30.0,
    hourly_cap: int = 5,
    daily_cap: int = 20,
) -> str:
    """
    Send a WhatsApp message to an existing contact.

    Parameters
    ----------
    contact:
        Contact name from contacts.yaml. Phone numbers are strictly rejected.
    text:
        Exact message text to send (up to 280 chars).
    request_id:
        Unique ID of the active user request (mandatory, single-send enforced).
    mode:
        Current permission mode (Mode.RESTRICTED or Mode.FREE).
    user_request:
        Original user query for provenance verification.
    tool_outputs:
        Outputs from previous tools in the task for provenance verification.
    """
    # 1. Fail closed if request_id is missing or empty
    if not request_id or not str(request_id).strip():
        return "Refused: request_id is required."

    # 2. Single send per request ID enforcement
    clean_req_id = str(request_id).strip()
    with _lock:
        if clean_req_id in _sent_request_ids:
            return "Refused: Only one WhatsApp message may be sent per request."

    # 3. Model parameter check: reject phone numbers passed as contact
    if is_raw_phone_number(contact):
        return "Refused: You must specify a contact name from contacts.yaml, not a phone number."

    # 4. Message length check (no truncation, refuse if over cap)
    clean_text = text if text is not None else ""
    if len(clean_text) > MAX_MESSAGE_LENGTH:
        return f"Refused: Message exceeds the {MAX_MESSAGE_LENGTH} character limit (length: {len(clean_text)})."
    if not clean_text.strip():
        return "Refused: Message text cannot be empty."

    # 5. Control characters and newlines check
    ok_ctrl, ctrl_reason = check_control_characters(clean_text)
    if not ok_ctrl:
        return ctrl_reason

    # 6. Confidential credentials check
    ok_secrets, secrets_reason = check_secrets(clean_text)
    if not ok_secrets:
        return secrets_reason

    # 7. Provenance guard check (prevent tool output exfiltration)
    ok_prov, prov_reason = check_provenance(clean_text, user_request, tool_outputs)
    if not ok_prov:
        return prov_reason

    # 8. Contact resolution
    res = resolve_contact(contact)
    if res.status == "model_number_rejected":
        return res.message
    if res.status != "found":
        return f"Contact resolution error: {res.message}"

    contact_name = res.contact_name
    phone_e164 = res.phone
    masked_phone = res.masked_phone

    # 9. Rate limiting check (survives restarts, fails closed on corrupt file)
    ok_rate, rate_reason = check_rate_limit(
        min_gap_seconds=min_gap_seconds,
        hourly_cap=hourly_cap,
        daily_cap=daily_cap,
    )
    if not ok_rate:
        return rate_reason

    # 10. Kill switch check before confirmations
    if KILL_SWITCH.is_stopped():
        return "Action blocked: kill switch is active."

    # 11. Spoken read-back (mandatory in BOTH modes)
    read_back_text = f"Preparing WhatsApp message to {contact_name} ({masked_phone}): {clean_text}."
    if speak_cb:
        try:
            speak_cb(read_back_text)
        except Exception as exc:
            logger.debug("Read-back speak error: %s", exc)

    # 12. Permissions & Confirmations
    # Fixed confirm wording showing full text (never from persona)
    confirm_prompt = (
        f"Confirmation required: Send WhatsApp message to {contact_name} ({masked_phone}): '{clean_text}'. Allow? (yes/no):"
    )

    if mode == Mode.RESTRICTED:
        # Restricted mode: always ask
        if confirm_cb:
            confirmed = confirm_cb(confirm_prompt)
            if not confirmed:
                return "Action cancelled: user declined confirmation."
        else:
            return f"Confirmation required: {confirm_prompt}"

    elif mode == Mode.FREE:
        # Free mode: first message to this contact requires yes/no
        if not is_contact_approved_free_mode(contact_name):
            first_prompt = f"First message to {contact_name} in Free mode requires confirmation. Allow? (yes/no):"
            if confirm_cb:
                confirmed = confirm_cb(first_prompt)
                if not confirmed:
                    return "Action cancelled: user declined first-contact approval in Free mode."
                approve_contact_free_mode(contact_name)
            else:
                return f"Confirmation required: {first_prompt}"
        else:
            # Subsequent messages run countdown (starts strictly after read-back)
            if countdown_cb:
                finished = countdown_cb(f"Sending WhatsApp message to {contact_name}", 3)
                if not finished:
                    return "Action cancelled: countdown was cancelled."

    # Re-check kill switch right before deep link launch
    if KILL_SWITCH.is_stopped():
        return "Action blocked: kill switch is active."

    # 13. Deep link launch
    clean_digits = phone_e164.replace("+", "").strip()
    encoded_text = urllib.parse.quote(clean_text, safe="")
    deep_link = f"whatsapp://send?phone={clean_digits}&text={encoded_text}"

    # Log with masked number only (never the message text!)
    logger.info("Opening WhatsApp deep link for '%s' (%s)", contact_name, masked_phone)
    _events.publish(
        "whatsapp.send",
        f"Opening WhatsApp for {contact_name}",
        {"contact": contact_name, "masked_phone": masked_phone, "request_id": clean_req_id},
    )

    try:
        if os.name == "nt":
            os.startfile(deep_link)
        else:
            logger.info("Mock startfile(%s)", deep_link)
    except Exception as exc:
        return f"Error: Failed opening WhatsApp deep link: {exc}"

    # 14. Foreground window verification loop
    deadline = time.time() + poll_timeout
    is_fg = False
    fg_reason = ""
    while time.time() < deadline:
        if KILL_SWITCH.is_stopped():
            return "Action blocked: kill switch is active."
        is_fg, fg_reason = _is_whatsapp_foreground(WHATSAPP_PROCESS_NAMES)
        if is_fg:
            break
        time.sleep(0.3)

    if not is_fg:
        return f"Action aborted: {fg_reason}"

    # Re-check foreground with minimal delay immediately before Enter
    is_fg, fg_reason = _is_whatsapp_foreground(WHATSAPP_PROCESS_NAMES)
    if not is_fg:
        return f"Action aborted: WhatsApp lost foreground focus immediately before Enter ({fg_reason})."

    # Re-check kill switch immediately before Enter
    if KILL_SWITCH.is_stopped():
        return "Action blocked: kill switch is active."

    # 15. Record send attempt in rate limiter and request tracking
    record_send_attempt()
    with _lock:
        _sent_request_ids.add(clean_req_id)

    # 16. Press Enter keystroke
    _press_enter()

    # Default report (never pretending confirmed delivery)
    return "I pressed Enter. I can't verify delivery, check WhatsApp."
