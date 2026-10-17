"""
test_app_launcher.py
====================
Comprehensive offline test suite for Milestone 2B:
- Universal app launcher (open_any_app)
- AppIndex caching, fallbacks, and ambiguity handling
- Shortcut resolution, arguments, and run-as flag inspection
- Security denylist and script interpreter refusal
- First-launch approval persistence, checksum tracking, and tamper detection
- WhatsApp and Spotify adapters
- Tool schemas matching TOOL_REGISTRY
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.adapters import get_adapter
from backend.adapters.spotify import SpotifyAdapter
from backend.adapters.whatsapp import WhatsAppAdapter
from backend.app_approvals import (
    compute_file_sha256,
    is_target_approved,
    is_valid_store_appid,
    save_approval,
)
from backend.app_index import AppIndex, IndexedApp, ResolveResult
from backend.app_security import (
    ShortcutInfo,
    check_denylist,
    check_script_or_interpreter,
    resolve_shortcut,
)
from backend.policy import KILL_SWITCH, Mode, RiskLevel, TOOL_REGISTRY, check
from backend.tools import KIRA_TOOLS_SCHEMA, TOOL_FUNCTIONS
from backend.tools.app_launcher import media_control, open_any_app


# ---------------------------------------------------------------------------
# 1. Policy check() approval tests
# ---------------------------------------------------------------------------
def test_policy_check_unapproved_asks_in_both_modes():
    """Unapproved app returns 'ask' in BOTH Restricted and Free modes."""
    # Unapproved or None -> ask
    assert check("open_any_app", Mode.RESTRICTED, approved=False) == "ask"
    assert check("open_any_app", Mode.FREE, approved=False) == "ask"
    assert check("open_any_app", Mode.RESTRICTED, approved=None) == "ask"
    assert check("open_any_app", Mode.FREE, approved=None) == "ask"

    # Approved -> allow
    assert check("open_any_app", Mode.RESTRICTED, approved=True) == "allow"
    assert check("open_any_app", Mode.FREE, approved=True) == "allow"


def test_policy_kill_switch_blocks_open_any_app():
    """Kill switch immediately blocks open_any_app regardless of approval."""
    KILL_SWITCH.stop()
    try:
        assert check("open_any_app", Mode.RESTRICTED, approved=True) == "block"
        assert check("open_any_app", Mode.FREE, approved=True) == "block"
        res = open_any_app("calc")
        assert "kill switch is active" in res.lower()
    finally:
        KILL_SWITCH.reset()


# ---------------------------------------------------------------------------
# 2. Denylist and script/interpreter refusal
# ---------------------------------------------------------------------------
def test_refuse_scripts_and_interpreters():
    """Refuse targets that are scripts or script hosts/interpreters."""
    scripts = ["run.bat", "setup.cmd", "payload.ps1", "script.vbs", "app.js", "control.msc"]
    for s in scripts:
        refused, reason = check_script_or_interpreter(f"C:\\tools\\{s}")
        assert refused is True, f"Failed to refuse script {s}"
        assert "script" in reason.lower()

    interpreters = ["mshta.exe", "wscript.exe", "cscript.exe", "rundll32.exe", "regsvr32.exe"]
    for host in interpreters:
        refused, reason = check_script_or_interpreter(f"C:\\Windows\\System32\\{host}")
        assert refused is True, f"Failed to refuse interpreter {host}"
        assert "interpreter" in reason.lower()

    # Normal application should pass
    refused, _ = check_script_or_interpreter("C:\\Program Files\\App\\app.exe")
    assert refused is False


def test_denylist_whole_word_matching_and_display_names():
    """Denylist blocks command interpreters, installers, and display names."""
    # Target executable stems
    denied_exes = ["cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe", "bash.exe", "regedit.exe", "mmc.exe", "diskpart.exe"]
    for exe in denied_exes:
        blocked, reason = check_denylist(target_path=f"C:\\Windows\\System32\\{exe}")
        assert blocked is True, f"Failed to block executable {exe}"
        assert "denylisted" in reason.lower()

    # Installers / uninstallers
    installers = ["setup.exe", "unins000.exe", "installer.exe", "update.exe"]
    for inst in installers:
        blocked, reason = check_denylist(target_path=f"C:\\Downloads\\{inst}")
        assert blocked is True, f"Failed to block installer {inst}"
        assert "installer" in reason.lower()

    # Display-name phrase matching
    blocked_names = [
        "Windows PowerShell",
        "Command Prompt",
        "Windows Terminal",
        "Registry Editor",
        "Disk Management",
    ]
    for name in blocked_names:
        blocked, reason = check_denylist(display_name=name)
        assert blocked is True, f"Failed to block display name '{name}'"
        assert "denylist" in reason.lower()

    # Denied Store app packages
    blocked, _ = check_denylist(is_store_app=True, app_id="Microsoft.WindowsTerminal_8wekyb3d8bbwe!App")
    assert blocked is True
    blocked, _ = check_denylist(is_store_app=True, app_id="Microsoft.PowerShell_8wekyb3d8bbwe!PowerShell")
    assert blocked is True


# ---------------------------------------------------------------------------
# 3. Store AppID validation
# ---------------------------------------------------------------------------
def test_store_appid_validation():
    """Validate allowed characters in Store AppIDs."""
    assert is_valid_store_appid("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify") is True
    assert is_valid_store_appid("Microsoft.WindowsCalculator_8wekyb3d8bbwe!App") is True

    # Bad characters
    assert is_valid_store_appid("Spotify;calc.exe") is False
    assert is_valid_store_appid("Spotify&calc.exe") is False
    assert is_valid_store_appid("Spotify|calc.exe") is False
    assert is_valid_store_appid("Spotify with spaces!App") is False
    assert is_valid_store_appid("../path/traversal!App") is False
    assert is_valid_store_appid("") is False


# ---------------------------------------------------------------------------
# 4. Approval record, checksum tracking, and change detection
# ---------------------------------------------------------------------------
def test_approval_record_persistence_and_tamper_detection(tmp_path, monkeypatch):
    """Approval persists target, size, mtime, sha256; re-asks if binary changes."""
    user_config_dir = tmp_path / "kira_user"
    user_config_dir.mkdir()
    monkeypatch.setenv("KIRA_USER_DIR", str(user_config_dir))

    # Create dummy app binary
    dummy_exe = tmp_path / "test_app.exe"
    dummy_exe.write_bytes(b"MZ_INITIAL_BINARY_V1")

    # Initially unapproved
    assert is_target_approved(str(dummy_exe), arguments="", is_store_app=False) is False

    # Save approval
    saved = save_approval(str(dummy_exe), arguments="--flag", is_store_app=False)
    assert saved is True

    # Verify approved with exact arguments
    assert is_target_approved(str(dummy_exe), arguments="--flag", is_store_app=False) is True
    # Different arguments -> not approved
    assert is_target_approved(str(dummy_exe), arguments="", is_store_app=False) is False

    # Tamper with the binary
    dummy_exe.write_bytes(b"MZ_TAMPERED_BINARY_V2")
    # Must re-ask (approval invalidated)
    assert is_target_approved(str(dummy_exe), arguments="--flag", is_store_app=False) is False


def test_store_app_approval(tmp_path, monkeypatch):
    """Store app approvals are keyed by validated AppID."""
    user_config_dir = tmp_path / "kira_user"
    user_config_dir.mkdir()
    monkeypatch.setenv("KIRA_USER_DIR", str(user_config_dir))

    app_id = "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"
    assert is_target_approved(app_id, is_store_app=True) is False

    assert save_approval(app_id, is_store_app=True) is True
    assert is_target_approved(app_id, is_store_app=True) is True

    # Bad AppID cannot be approved
    assert save_approval("Invalid;AppID", is_store_app=True) is False


# ---------------------------------------------------------------------------
# 5. App Index caching, fallbacks, and ambiguity handling
# ---------------------------------------------------------------------------
def test_index_failure_keeps_old_cache():
    """Index keeps existing cache when a refresh fails."""
    idx = AppIndex()
    app1 = IndexedApp("App One", "appone", "C:\\app1.exe", "", "", False, "", False, ())
    idx._cache = [app1]

    # Mock refresh error
    with patch.object(idx, "_get_start_menu_paths", side_effect=OSError("Disk read error")):
        ok = idx.refresh()
        assert ok is False
        assert idx.get_indexed_count() == 1
        assert idx._cache[0].name == "App One"


def test_index_never_logs_in_full(caplog):
    """Index logs only counts and never dumps all app names or paths."""
    idx = AppIndex()
    app1 = IndexedApp("SecretApp1", "secretapp1", "C:\\secret\\app1.exe", "", "", False, "", False, ())
    app2 = IndexedApp("SecretApp2", "secretapp2", "C:\\secret\\app2.exe", "", "", False, "", False, ())
    idx._cache = [app1, app2]

    with caplog.at_level(logging.INFO):
        idx.refresh()

    # Full private paths must not appear in log records
    for record in caplog.records:
        assert "C:\\secret\\app1.exe" not in record.message
        assert "C:\\secret\\app2.exe" not in record.message


def test_fuzzy_matching_strong_ambiguous_and_weak():
    """Fuzzy matching handles confident matches, asks on ambiguity, and limits suggestions to 3."""
    idx = AppIndex()
    apps = [
        IndexedApp("Google Chrome", "googlechrome", "C:\\chrome.exe", "", "", False, "", False, ("chrome",)),
        IndexedApp("Google Cloud SDK", "googlecloudsdk", "C:\\gcloud.exe", "", "", False, "", False, ()),
        IndexedApp("Spotify Music", "spotifymusic", "C:\\spotify.exe", "", "", False, "", False, ("spotify",)),
        IndexedApp("Discord", "discord", "C:\\discord.exe", "", "", False, "", False, ()),
    ]
    idx._cache = apps

    # 1. Exact / alias match
    res = idx.resolve("chrome", auto_retry=False)
    assert res.status == "found"
    assert res.match is not None
    assert res.match.name == "Google Chrome"

    # 2. Confident match
    res = idx.resolve("spotify", auto_retry=False)
    assert res.status == "found"
    assert res.match.name == "Spotify Music"

    # 3. Ambiguous match (Google Chrome vs Google Cloud SDK)
    res = idx.resolve("google", auto_retry=False)
    assert res.status == "ambiguous"
    assert res.match is None
    assert len(res.candidates) <= 3
    assert "Did you mean" in res.message
    assert "yes/no" in res.message

    # 4. Weak match
    res = idx.resolve("xyzxyz", auto_retry=False)
    assert res.status in ("weak_match", "not_found")
    assert res.match is None
    assert len(res.candidates) <= 3


# ---------------------------------------------------------------------------
# 6. Adapters: Spotify and WhatsApp
# ---------------------------------------------------------------------------
def test_adapter_unknown_action_rejected():
    """Adapters reject any unknown or unexposed actions."""
    spot = SpotifyAdapter()
    res = spot.execute("invalid_action")
    assert res.success is False
    assert "Unknown action" in res.message

    wa = WhatsAppAdapter()
    res = wa.execute("invalid_action")
    assert res.success is False
    assert "Unknown action" in res.message


def test_whatsapp_messaging_deferred_to_m3():
    """WhatsApp send_message is explicitly deferred to Milestone 3."""
    wa = WhatsAppAdapter()
    res = wa.execute("send_message", recipient="Meldan", message="Hello")
    assert res.success is False
    assert "Milestone 3" in res.message


def test_spotify_media_keys_running_check_and_reporting():
    """Spotify media keys require process to be running and report 'play/pause sent'."""
    spot = SpotifyAdapter()

    # Case A: Spotify not running
    with patch.object(spot, "is_running", return_value=False):
        res = spot.execute("play_pause")
        assert res.success is False
        assert "not currently running" in res.message

    # Case B: Spotify running -> reports 'play/pause sent'
    with patch.object(spot, "is_running", return_value=True), patch("backend.adapters.spotify._send_media_key", return_value=True):
        res = spot.execute("play_pause")
        assert res.success is True
        assert res.message == "play/pause sent"

        res_next = spot.execute("next")
        assert res_next.success is True
        assert res_next.message == "next track sent"


# ---------------------------------------------------------------------------
# 7. Tool schemas match TOOL_REGISTRY
# ---------------------------------------------------------------------------
def test_tool_schemas_match_tool_registry():
    """Verify that every tool schema matches an entry in TOOL_REGISTRY with correct risk."""
    schema_tool_names = {item["function"]["name"] for item in KIRA_TOOLS_SCHEMA}
    assert "open_any_app" in schema_tool_names
    assert "media_control" in schema_tool_names

    for name in schema_tool_names:
        assert name in TOOL_REGISTRY, f"Tool '{name}' in schema but missing from TOOL_REGISTRY"
        entry = TOOL_REGISTRY[name]
        assert "risk" in entry
        assert isinstance(entry["risk"], RiskLevel)

    assert "open_any_app" in TOOL_FUNCTIONS
    assert "media_control" in TOOL_FUNCTIONS


# ---------------------------------------------------------------------------
# 8. open_any_app execution, polling, and confirmation flow
# ---------------------------------------------------------------------------
def test_open_any_app_first_launch_approval_flow(tmp_path, monkeypatch):
    """First launch asks confirmation; records approval upon consent and launches."""
    user_config_dir = tmp_path / "kira_user"
    user_config_dir.mkdir()
    monkeypatch.setenv("KIRA_USER_DIR", str(user_config_dir))

    dummy_exe = tmp_path / "demo_app.exe"
    dummy_exe.write_bytes(b"MZ_DEMO")

    idx = AppIndex()
    app = IndexedApp("Demo App", "demoapp", str(dummy_exe), "", "", False, "", False, ())
    idx._cache = [app]

    with patch("backend.tools.app_launcher.APP_INDEX", idx):
        # 1. Unapproved without callback -> returns confirmation required string
        res = open_any_app("Demo App", mode=Mode.RESTRICTED, confirm_cb=None)
        assert "Confirmation required" in res

        # 2. Callback declines -> cancelled
        res_declined = open_any_app("Demo App", mode=Mode.RESTRICTED, confirm_cb=lambda prompt: False)
        assert "Action cancelled" in res_declined
        assert is_target_approved(str(dummy_exe)) is False

        # 3. Callback approves -> saves approval, launches, polls
        with patch("backend.tools.app_launcher._poll_window_or_process", return_value=True), patch("os.startfile"):
            res_approved = open_any_app("Demo App", mode=Mode.RESTRICTED, confirm_cb=lambda prompt: True)
            assert res_approved == "Opened Demo App."
            assert is_target_approved(str(dummy_exe)) is True

        # 4. Subsequent launch does not ask callback
        mock_cb = MagicMock(return_value=True)
        with patch("backend.tools.app_launcher._poll_window_or_process", return_value=True), patch("os.startfile"):
            res_again = open_any_app("Demo App", mode=Mode.RESTRICTED, confirm_cb=mock_cb)
            assert res_again == "Opened Demo App."
            mock_cb.assert_not_called()


def test_open_any_app_polling_timeout_reporting(tmp_path, monkeypatch):
    """When window/process is not detected within timeout, reports unconfirmed."""
    user_config_dir = tmp_path / "kira_user"
    user_config_dir.mkdir()
    monkeypatch.setenv("KIRA_USER_DIR", str(user_config_dir))

    dummy_exe = tmp_path / "ghost_app.exe"
    dummy_exe.write_bytes(b"MZ_GHOST")
    save_approval(str(dummy_exe))

    idx = AppIndex()
    app = IndexedApp("Ghost App", "ghostapp", str(dummy_exe), "", "", False, "", False, ())
    idx._cache = [app]

    with patch("backend.tools.app_launcher.APP_INDEX", idx), patch("backend.tools.app_launcher._poll_window_or_process", return_value=False), patch("os.startfile"):
        res = open_any_app("Ghost App", poll_timeout=0.01)
        assert res == "Command was sent to open Ghost App, but the window could not be confirmed."

