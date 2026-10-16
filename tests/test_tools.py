"""
test_tools.py
=============
Unit tests for Kira tools in backend/tools/.

Tests Covered
-------------
- create_folder / create_file inside workspace and escape prevention
- run_command:
  - allowlisted execution
  - shell metacharacter and chaining blocking (&, |, >, <, ;, etc.)
  - path argument escaping blocked via resolve_path
  - kill switch activation before and during command execution
- open_app:
  - allowlisted app launch
  - disallowed app rejection
  - workspace boundary enforcement for code and explorer
- open_website:
  - HTTPS-only enforcement (HTTP rejected)
  - exact domain and true subdomain allow-listing
  - lookalike domains (e.g. notgithub.com, github.com.evil.com) rejected
- speak stub
- tool schemas match TOOL_REGISTRY
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import backend.policy as policy_module
from backend.policy import KILL_SWITCH, PolicyError, TOOL_REGISTRY
from backend.tools import KIRA_TOOLS_SCHEMA, TOOL_FUNCTIONS
from backend.tools.execution import (
    _get_pause_seconds,
    has_shell_metachars,
    is_command_allowlisted,
    run_command,
)
from backend.tools.filesystem import create_file, create_folder
from backend.tools.system import is_allowed_host, open_app, open_website
from backend.tools.voice_stub import speak


@pytest.fixture(autouse=True)
def reset_kill_switch():
    KILL_SWITCH.reset()
    yield
    KILL_SWITCH.reset()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    resolved = tmp_path.resolve()
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", resolved)
    return resolved


# ---------------------------------------------------------------------------
# Filesystem tools
# ---------------------------------------------------------------------------


def test_create_folder_inside_workspace(workspace: Path):
    res = create_folder("demo_folder")
    assert "successfully" in res.lower()
    assert (workspace / "demo_folder").is_dir()


def test_create_folder_escape_blocked(workspace: Path):
    with pytest.raises(PolicyError):
        create_folder("../escaped_folder")


def test_create_file_inside_workspace(workspace: Path):
    res = create_file("demo_folder/hello.py", "print('Hello, World!')")
    assert "successfully" in res.lower()
    target = workspace / "demo_folder" / "hello.py"
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "print('Hello, World!')"


def test_create_file_escape_blocked(workspace: Path):
    with pytest.raises(PolicyError):
        create_file("../escaped.py", "malicious code")


# ---------------------------------------------------------------------------
# Execution tool
# ---------------------------------------------------------------------------


def test_is_command_allowlisted():
    assert is_command_allowlisted("python demo/hello.py") is True
    assert is_command_allowlisted("python.exe demo/hello.py") is True
    assert is_command_allowlisted("pytest -v") is True
    assert is_command_allowlisted("dir") is True
    assert is_command_allowlisted("format C:") is False
    assert is_command_allowlisted("powershell -enc ...") is False


def test_shell_chaining_blocked():
    assert has_shell_metachars("dir & whoami") is True
    assert has_shell_metachars("python test.py | more") is True
    assert has_shell_metachars("dir > output.txt") is True
    assert has_shell_metachars("echo hi < input.txt") is True
    assert has_shell_metachars("dir ; whoami") is True
    assert has_shell_metachars("python\ntest.py") is True

    res = run_command("dir & whoami")
    assert "shell metacharacters or command chaining detected" in res.lower()


def test_run_command_path_escape_blocked(workspace: Path):
    res = run_command("python ../outside.py")
    assert "escapes the workspace" in res.lower()

    res2 = run_command("rmdir /s /q C:\\")
    assert "escapes the workspace" in res2.lower()


def test_run_command_allowlisted_execution(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", workspace)

    script = workspace / "test.py"
    script.write_text("print('KIRA_TEST_OUTPUT')", encoding="utf-8")

    # Run visible=False in automated test
    res = run_command("python test.py", visible=False)
    assert "KIRA_TEST_OUTPUT" in res


def test_kill_switch_before_run_command(workspace: Path):
    KILL_SWITCH.stop()
    res = run_command("python -c \"print('test')\"", visible=False)
    assert "halted: kill switch is active" in res.lower()


def test_kill_switch_during_run_command(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", workspace)

    script = workspace / "slow.py"
    script.write_text("import time; time.sleep(2.0); print('DONE')", encoding="utf-8")

    # Activate kill switch in a background thread while run_command is executing
    def trigger_stop():
        time.sleep(0.1)
        KILL_SWITCH.stop()

    t = threading.Thread(target=trigger_stop, daemon=True)
    t.start()

    res = run_command("python slow.py", visible=False, timeout_seconds=5.0)
    assert "halted: kill switch was activated" in res.lower()


# ---------------------------------------------------------------------------
# System tools: open_app
# ---------------------------------------------------------------------------


def test_open_app_allowlisted():
    with patch("subprocess.Popen") as mock_popen:
        res = open_app("notepad")
        assert "launched" in res.lower()
        mock_popen.assert_called_once()
        # Verify shell=False
        assert mock_popen.call_args[1].get("shell") is False


def test_open_app_disallowed():
    res = open_app("malicious_hacker_tool")
    assert "not in the allow-list" in res.lower()


def test_code_and_explorer_workspace_boundary(workspace: Path):
    # Valid relative path inside workspace
    with patch("subprocess.Popen") as mock_popen:
        res = open_app("code", path="demo")
        assert "launched" in res.lower()
        called_path = str(mock_popen.call_args[0][0][1])
        assert str(workspace) in called_path

    # Path escaping workspace must raise PolicyError
    with pytest.raises(PolicyError):
        open_app("code", path="../escaped")

    with pytest.raises(PolicyError):
        open_app("explorer", path="C:\\Windows")


# ---------------------------------------------------------------------------
# System tools: open_website & lookalike rejection
# ---------------------------------------------------------------------------


def test_open_website_allowlisted():
    with patch("webbrowser.open") as mock_open:
        res = open_website("https://github.com/ponram06/kira")
        assert "opened website" in res.lower()
        mock_open.assert_called_once_with("https://github.com/ponram06/kira")


def test_open_website_disallowed():
    res = open_website("https://sketchy-untrusted-site.xyz")
    assert "not in the allow-list" in res.lower()


def test_lookalike_domain_rejected():
    # Insecure HTTP scheme rejected
    res_http = open_website("http://github.com")
    assert "insecure url rejected" in res_http.lower()

    # Lookalike domains rejected
    res_lookalike1 = open_website("https://notgithub.com")
    assert "not in the allow-list" in res_lookalike1.lower()

    res_lookalike2 = open_website("https://github.com.evil.com")
    assert "not in the allow-list" in res_lookalike2.lower()

    # Subdomain check helper
    allowed = ["github.com", "google.com"]
    assert is_allowed_host("github.com", allowed) is True
    assert is_allowed_host("api.github.com", allowed) is True
    assert is_allowed_host("gist.github.com", allowed) is True
    assert is_allowed_host("notgithub.com", allowed) is False
    assert is_allowed_host("github.com.evil.com", allowed) is False
    assert is_allowed_host("fakegoogle.com", allowed) is False


# ---------------------------------------------------------------------------
# Voice stub
# ---------------------------------------------------------------------------


def test_speak_stub(capsys: pytest.CaptureFixture):
    res = speak("Task completed successfully")
    captured = capsys.readouterr()
    assert "[Kira speaking]: Task completed successfully" in captured.out
    assert "Spoke:" in res


# ---------------------------------------------------------------------------
# Tool schemas match TOOL_REGISTRY
# ---------------------------------------------------------------------------


def test_tool_schemas_match_tool_registry():
    """Verify that every tool schema exposed to LLMs exists in TOOL_REGISTRY."""
    assert len(KIRA_TOOLS_SCHEMA) > 0

    for item in KIRA_TOOLS_SCHEMA:
        fn_name = item["function"]["name"]
        assert fn_name in TOOL_REGISTRY, f"Schema tool '{fn_name}' not in TOOL_REGISTRY"
        assert fn_name in TOOL_FUNCTIONS, f"Schema tool '{fn_name}' has no registered function"


def test_cmd_pause_seconds(tmp_path: Path):
    """Test reading cmd_pause_seconds from config and falling back gracefully."""
    # Custom config with 15s pause
    cfg_file = tmp_path / "custom_config.yaml"
    cfg_file.write_text("tools:\n  cmd_pause_seconds: 15\n", encoding="utf-8")
    assert _get_pause_seconds(cfg_file) == 15

    # Non-existent config falls back to default 20s
    missing_file = tmp_path / "nonexistent.yaml"
    assert _get_pause_seconds(missing_file) == 20

