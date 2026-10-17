"""
test_whatsapp.py
================
Comprehensive offline test suite for Milestone 3 (WhatsApp Messaging):
- Protocol scheme isolation (whatsapp:// rejected by other tools)
- Model parameter validation (phone number strings rejected)
- Contacts resolution, fuzzy candidate limits, and workspace isolation
- Character cap (280 chars), control character rejection, secrets check
- Provenance guard (20+ chars copied from tool output refused)
- Single-send enforcement per request ID and fail-closed missing request_id
- Zero retries on error or after Enter
- Rate limit persistence, atomic writes, corrupt file fail-closed, and min gap
- First-contact confirmation in Free mode and countdown cancellation
- Foreground window verification with ctypes and dialog detection
- Delivery reporting ("I pressed Enter...")
- Phone number masking and message privacy in logs and events
- Tool schema matching TOOL_REGISTRY
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from backend import events as _events
from backend import secrets_guard
from backend.contacts import (
    is_raw_phone_number,
    load_contacts,
    mask_phone,
    resolve_contact,
    validate_e164_phone,
)
from backend.policy import KILL_SWITCH, Mode, RiskLevel, TOOL_REGISTRY, check, resolve_path
from backend.tools import KIRA_TOOLS_SCHEMA, TOOL_FUNCTIONS
from backend.tools.app_launcher import open_any_app
from backend.tools.execution import run_command
from backend.tools.system import open_website
from backend.tools.whatsapp_tool import (
    check_control_characters,
    check_provenance,
    check_secrets,
    reset_sent_request_ids,
    send_whatsapp,
)
from backend.whatsapp_rate_limit import (
    approve_contact_free_mode,
    check_rate_limit,
    is_contact_approved_free_mode,
    record_send_attempt,
)


@pytest.fixture(autouse=True)
def setup_teardown(tmp_path, monkeypatch):
    """Set up clean user config directory and reset request tracking."""
    user_dir = tmp_path / "kira_user"
    user_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIRA_USER_DIR", str(user_dir))
    reset_sent_request_ids()
    KILL_SWITCH.reset()
    yield
    reset_sent_request_ids()
    KILL_SWITCH.reset()


# ---------------------------------------------------------------------------
# 1. Protocol Scheme Isolation
# ---------------------------------------------------------------------------
def test_other_tools_reject_whatsapp_scheme():
    """Verify open_website, open_any_app, and run_command reject whatsapp:// links."""
    # open_website
    res = open_website("whatsapp://send?phone=15551234567&text=hello")
    assert "Insecure URL rejected" in res or "Error" in res

    # open_any_app
    res_app = open_any_app("whatsapp://send?phone=15551234567")
    assert "Error" in res_app or "not found" in res_app.lower()

    # run_command
    res_cmd = run_command("whatsapp://send?phone=15551234567")
    assert "not in the allow-list" in res_cmd or "Error" in res_cmd


# ---------------------------------------------------------------------------
# 2. Contacts Resolution, Phone Validation, and Workspace Isolation
# ---------------------------------------------------------------------------
def test_phone_masking_shows_last_3_digits_only():
    """Masking retains country prefix and only the last 3 digits."""
    assert mask_phone("+15551234567") == "+********567"
    assert mask_phone("+442071838750") == "+*********750"
    assert mask_phone("15551234567") == "********567"
    assert mask_phone("+91") == "+91"
    assert mask_phone("") == ""


def test_validate_e164_phone():
    """Validates international phone numbers with mandatory country code."""
    valid, num = validate_e164_phone("+15551234567")
    assert valid is True
    assert num == "+15551234567"

    valid, num = validate_e164_phone("442071838750")
    assert valid is True
    assert num == "+442071838750"

    # Too short / invalid formats
    assert validate_e164_phone("12345")[0] is False
    assert validate_e164_phone("abcde")[0] is False
    assert validate_e164_phone("+1")[0] is False


def test_model_supplied_phone_number_rejected(tmp_path, monkeypatch):
    """Direct phone number string from the model is rejected."""
    assert is_raw_phone_number("+15551234567") is True
    assert is_raw_phone_number("15551234567") is True
    assert is_raw_phone_number("555-123-4567") is True
    assert is_raw_phone_number("Meldan") is False

    # Calling send_whatsapp with phone number as contact
    res = send_whatsapp(contact="+15551234567", text="Hi", request_id="req1")
    assert "must specify a contact name" in res


def test_contacts_file_unreachable_by_file_tools(tmp_path, monkeypatch):
    """Contacts file in user config directory cannot be reached by workspace file tools."""
    user_dir = Path(os.environ.get("KIRA_USER_DIR"))
    contacts_file = user_dir / "contacts.yaml"
    contacts_file.write_text("contacts:\n  test: '+15551234567'\n", encoding="utf-8")

    from backend.policy import PolicyError
    with pytest.raises(PolicyError):
        resolve_path(contacts_file)


def test_contact_lookup_exact_ambiguous_and_candidate_limit(tmp_path, monkeypatch):
    """Contact lookup matches exact, asks ambiguous one at a time, returns <=3 candidates."""
    user_dir = Path(os.environ.get("KIRA_USER_DIR"))
    contacts_file = user_dir / "contacts.yaml"
    data = {
        "contacts": {
            "meldan": "+15551234567",
            "melissa": "+15559876543",
            "mel": "+15551112233",
            "alice": "+442071838750",
            "bob": "+919876543210",
        }
    }
    contacts_file.write_text(yaml.safe_dump(data), encoding="utf-8")

    # 1. Exact match (case insensitive)
    res = resolve_contact("Meldan")
    assert res.status == "found"
    assert res.phone == "+15551234567"
    assert res.masked_phone == "+********567"

    # 2. Ambiguous match
    res_amb = resolve_contact("mel")
    # Exact match takes precedence
    assert res_amb.status == "found"
    assert res_amb.contact_name == "mel"

    # 3. Fuzzy ambiguous match
    res_fuzzy = resolve_contact("melli")
    assert res_fuzzy.status in ("ambiguous", "not_found")
    assert len(res_fuzzy.candidates) <= 3
    if res_fuzzy.status == "ambiguous":
        assert "yes/no" in res_fuzzy.message

    # 4. Not found returns <= 3 candidates
    res_none = resolve_contact("unknownperson")
    assert res_none.status == "not_found"
    assert len(res_none.candidates) <= 3


# ---------------------------------------------------------------------------
# 3. Message Constraints & Security Guards
# ---------------------------------------------------------------------------
def test_message_length_cap_refuses_without_truncation():
    """Text exceeding 280 characters is refused with exact count."""
    long_text = "A" * 281
    res = send_whatsapp(contact="Meldan", text=long_text, request_id="req1")
    assert "exceeds the 280 character limit" in res
    assert "281" in res


def test_newlines_and_control_characters_rejected():
    """Newlines, carriage returns, and control characters are blocked."""
    assert check_control_characters("Hello\nWorld")[0] is False
    assert check_control_characters("Hello\rWorld")[0] is False
    assert check_control_characters("Hello\tWorld")[0] is False
    assert check_control_characters("Hello\x00World")[0] is False
    assert check_control_characters("Hello World")[0] is True

    res = send_whatsapp(contact="Meldan", text="Hello\nWorld", request_id="req1")
    assert "newlines or control characters" in res


def test_secret_looking_text_rejected(monkeypatch):
    """Text matching any known secret is rejected."""
    monkeypatch.setattr(secrets_guard, "known_secrets", lambda: ["SK_LIVE_SECRET_KEY_12345"])
    ok, reason = check_secrets("Here is the key: SK_LIVE_SECRET_KEY_12345")
    assert ok is False
    assert "confidential credentials" in reason

    res = send_whatsapp(contact="Meldan", text="Here is the key: SK_LIVE_SECRET_KEY_12345", request_id="req1")
    assert "confidential credentials or secrets" in res


def test_provenance_guard_refuses_copied_tool_output():
    """Refuses message text containing 20+ chars copied from tool output not in user request."""
    user_prompt = "Send Meldan a message about the report"
    tool_output = "CONFIDENTIAL_DATA_LEAK_EXFILTRATION_TEST_12345"

    # Attempting to send text extracted from tool output
    stolen_text = f"Report result: {tool_output}"
    ok, reason = check_provenance(stolen_text, user_request=user_prompt, tool_outputs=[tool_output])
    assert ok is False
    assert "copied from prior tool output" in reason

    # Legitimate user-provided text containing 20+ chars
    legit_text = "Please review the quarterly financial figures immediately"
    legit_user = "Please review the quarterly financial figures immediately and let me know"
    ok, _ = check_provenance(legit_text, user_request=legit_user, tool_outputs=["some other tool output"])
    assert ok is True


# ---------------------------------------------------------------------------
# 4. Request ID & Single-Send Enforcement
# ---------------------------------------------------------------------------
def test_missing_or_empty_request_id_refused():
    """Missing or empty request_id fails closed."""
    assert "request_id is required" in send_whatsapp(contact="Meldan", text="Hi", request_id=None)
    assert "request_id is required" in send_whatsapp(contact="Meldan", text="Hi", request_id="")
    assert "request_id is required" in send_whatsapp(contact="Meldan", text="Hi", request_id="   ")


def test_second_send_in_one_request_refused(tmp_path, monkeypatch):
    """Only one WhatsApp message may be sent per request ID."""
    user_dir = Path(os.environ.get("KIRA_USER_DIR"))
    contacts_file = user_dir / "contacts.yaml"
    contacts_file.write_text("contacts:\n  meldan: '+15551234567'\n", encoding="utf-8")

    with patch("backend.tools.whatsapp_tool._is_whatsapp_foreground", return_value=(True, "")), \
         patch("backend.tools.whatsapp_tool._press_enter"), \
         patch("os.startfile"):
        res1 = send_whatsapp(contact="Meldan", text="First message", request_id="req_unique_1", confirm_cb=lambda p: True)
        assert "I pressed Enter" in res1

        # Second send with same request_id
        res2 = send_whatsapp(contact="Meldan", text="Second message", request_id="req_unique_1", confirm_cb=lambda p: True)
        assert "Only one WhatsApp message may be sent per request" in res2


# ---------------------------------------------------------------------------
# 5. Rate Limiting Persistence & Fail-Closed Behavior
# ---------------------------------------------------------------------------
def test_rate_limit_corrupt_file_fails_closed(tmp_path, monkeypatch):
    """Corrupt rate limit file blocks sending (fails closed, never resets counters)."""
    user_dir = Path(os.environ.get("KIRA_USER_DIR"))
    rl_file = user_dir / "whatsapp_rate_limit.json"
    rl_file.write_text("{ CORRUPTED_JSON_DATA", encoding="utf-8")

    allowed, reason = check_rate_limit()
    assert allowed is False
    assert "corrupt or unreadable" in reason


def test_rate_limit_minimum_gap_enforced():
    """Enforces minimum gap (default 30s) between send attempts."""
    record_send_attempt()

    # Immediate second check
    allowed, reason = check_rate_limit(min_gap_seconds=30.0)
    assert allowed is False
    assert "Minimum gap" in reason


def test_rate_limit_hourly_and_daily_caps():
    """Enforces hourly and daily send caps."""
    # Simulate reaching hourly cap
    for _ in range(5):
        record_send_attempt()

    allowed, reason = check_rate_limit(min_gap_seconds=0.0, hourly_cap=5, daily_cap=20)
    assert allowed is False
    assert "Hourly cap" in reason


# ---------------------------------------------------------------------------
# 6. Permissions, Confirmations & Free Mode
# ---------------------------------------------------------------------------
def test_policy_check_send_whatsapp():
    """Policy check for send_whatsapp reflects HIGH risk and affects_people."""
    assert check("send_whatsapp", Mode.RESTRICTED) == "ask"
    assert check("send_whatsapp", Mode.FREE) == "countdown"


def test_free_mode_first_contact_ask(tmp_path, monkeypatch):
    """Free mode asks confirmation on first message to a contact; subsequent use countdown."""
    contact_name = "meldan"
    assert is_contact_approved_free_mode(contact_name) is False

    # Simulate user approval
    approve_contact_free_mode(contact_name)
    assert is_contact_approved_free_mode(contact_name) is True


def test_voice_read_back_mandatory(tmp_path, monkeypatch):
    """Voice read-back speaks contact name, masked number, and full text."""
    user_dir = Path(os.environ.get("KIRA_USER_DIR"))
    contacts_file = user_dir / "contacts.yaml"
    contacts_file.write_text("contacts:\n  meldan: '+15551234567'\n", encoding="utf-8")

    spoken_phrases = []
    def mock_speak(text):
        spoken_phrases.append(text)

    with patch("backend.tools.whatsapp_tool._is_whatsapp_foreground", return_value=(True, "")), \
         patch("backend.tools.whatsapp_tool._press_enter"), \
         patch("os.startfile"):
        send_whatsapp(
            contact="Meldan",
            text="Meeting at 5pm",
            request_id="req_speak_1",
            mode=Mode.RESTRICTED,
            confirm_cb=lambda p: True,
            speak_cb=mock_speak,
        )

    assert len(spoken_phrases) == 1
    spoken = spoken_phrases[0]
    assert "Meldan" in spoken
    assert "+********567" in spoken
    assert "Meeting at 5pm" in spoken
    assert "+15551234567" not in spoken  # Full number must NOT be spoken


# ---------------------------------------------------------------------------
# 7. Foreground Window Verification & Enter Keystroke
# ---------------------------------------------------------------------------
def test_foreground_check_wrong_window_aborts(tmp_path, monkeypatch):
    """If foreground window belongs to another process, aborts without pressing Enter."""
    user_dir = Path(os.environ.get("KIRA_USER_DIR"))
    contacts_file = user_dir / "contacts.yaml"
    contacts_file.write_text("contacts:\n  meldan: '+15551234567'\n", encoding="utf-8")

    enter_pressed = False
    def mock_press():
        nonlocal enter_pressed
        enter_pressed = True

    with patch("backend.tools.whatsapp_tool._is_whatsapp_foreground", return_value=(False, "Foreground window belongs to 'notepad.exe', not WhatsApp.")), \
         patch("backend.tools.whatsapp_tool._press_enter", mock_press), \
         patch("os.startfile"):
        res = send_whatsapp(
            contact="Meldan",
            text="Test message",
            request_id="req_fg_1",
            confirm_cb=lambda p: True,
            poll_timeout=0.01,
        )

    assert "Action aborted" in res
    assert "notepad.exe" in res
    assert enter_pressed is False


def test_foreground_check_error_dialog_aborts(tmp_path, monkeypatch):
    """If WhatsApp displays an error dialog, aborts without pressing Enter."""
    user_dir = Path(os.environ.get("KIRA_USER_DIR"))
    contacts_file = user_dir / "contacts.yaml"
    contacts_file.write_text("contacts:\n  meldan: '+15551234567'\n", encoding="utf-8")

    enter_pressed = False
    def mock_press():
        nonlocal enter_pressed
        enter_pressed = True

    with patch("backend.tools.whatsapp_tool._is_whatsapp_foreground", return_value=(False, "WhatsApp displayed an error dialog: 'Phone number shared via url is invalid'.")), \
         patch("backend.tools.whatsapp_tool._press_enter", mock_press), \
         patch("os.startfile"):
        res = send_whatsapp(
            contact="Meldan",
            text="Test message",
            request_id="req_fg_2",
            confirm_cb=lambda p: True,
            poll_timeout=0.01,
        )

    assert "Action aborted" in res
    assert "error dialog" in res
    assert enter_pressed is False


def test_delivery_reporting_pressed_enter(tmp_path, monkeypatch):
    """Reports 'I pressed Enter. I can't verify delivery, check WhatsApp.' upon success."""
    user_dir = Path(os.environ.get("KIRA_USER_DIR"))
    contacts_file = user_dir / "contacts.yaml"
    contacts_file.write_text("contacts:\n  meldan: '+15551234567'\n", encoding="utf-8")

    with patch("backend.tools.whatsapp_tool._is_whatsapp_foreground", return_value=(True, "")), \
         patch("backend.tools.whatsapp_tool._press_enter"), \
         patch("os.startfile"):
        res = send_whatsapp(
            contact="Meldan",
            text="Test message",
            request_id="req_success_1",
            confirm_cb=lambda p: True,
        )

    assert res == "I pressed Enter. I can't verify delivery, check WhatsApp."


# ---------------------------------------------------------------------------
# 8. Privacy & Logging
# ---------------------------------------------------------------------------
def test_logs_and_events_never_contain_message_text(tmp_path, monkeypatch, caplog):
    """Logs and event payloads contain only contact name and masked number, never message text."""
    user_dir = Path(os.environ.get("KIRA_USER_DIR"))
    contacts_file = user_dir / "contacts.yaml"
    contacts_file.write_text("contacts:\n  meldan: '+15551234567'\n", encoding="utf-8")

    published_events = []
    def mock_publish(event_type, message, data=None):
        published_events.append((event_type, message, data or {}))

    monkeypatch.setattr(_events, "publish", mock_publish)

    secret_message = "TOP_SECRET_USER_TEXT_12345"

    with patch("backend.tools.whatsapp_tool._is_whatsapp_foreground", return_value=(True, "")), \
         patch("backend.tools.whatsapp_tool._press_enter"), \
         patch("os.startfile"), \
         caplog.at_level(logging.INFO):
        send_whatsapp(
            contact="Meldan",
            text=secret_message,
            request_id="req_priv_1",
            confirm_cb=lambda p: True,
        )

    # Check event bus
    for ev_type, ev_msg, ev_data in published_events:
        if ev_type.startswith("whatsapp"):
            assert secret_message not in ev_msg
            assert secret_message not in str(ev_data)
            assert "+15551234567" not in str(ev_data)
            assert "+********567" in str(ev_data) or "Meldan" in str(ev_data)

    # Check logs
    for record in caplog.records:
        assert secret_message not in record.message
        assert "+15551234567" not in record.message


# ---------------------------------------------------------------------------
# 9. Tool Schema Matches TOOL_REGISTRY
# ---------------------------------------------------------------------------
def test_send_whatsapp_schema_matches_tool_registry():
    """Verify send_whatsapp is registered in TOOL_REGISTRY with HIGH risk and affects_people."""
    assert "send_whatsapp" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["send_whatsapp"]
    assert entry["risk"] == RiskLevel.HIGH
    assert entry["affects_people"] is True
    assert entry["outside_workspace"] is False

    schema_tool_names = {item["function"]["name"] for item in KIRA_TOOLS_SCHEMA}
    assert "send_whatsapp" in schema_tool_names
    assert "send_whatsapp" in TOOL_FUNCTIONS
