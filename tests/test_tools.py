"""
test_tools.py
=============
Unit tests for Kira tools in backend/tools/.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import backend.policy as policy_module
from backend.policy import PolicyError
from backend.tools.execution import is_command_allowlisted, run_command
from backend.tools.filesystem import create_file, create_folder
from backend.tools.system import open_app, open_website
from backend.tools.voice_stub import speak


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


def test_run_command_non_allowlisted_rejected(workspace: Path):
    res = run_command("rmdir /s /q C:\\")
    assert "not in the allow-list" in res.lower()


def test_run_command_allowlisted_execution(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", workspace)

    script = workspace / "test.py"
    script.write_text("print('KIRA_TEST_OUTPUT')", encoding="utf-8")

    # Run visible=False in automated test
    res = run_command("python test.py", visible=False)
    assert "KIRA_TEST_OUTPUT" in res


# ---------------------------------------------------------------------------
# System tools
# ---------------------------------------------------------------------------


def test_open_app_allowlisted():
    with patch("subprocess.Popen") as mock_popen:
        res = open_app("notepad")
        assert "launched" in res.lower()
        mock_popen.assert_called_once()


def test_open_app_disallowed():
    res = open_app("malicious_hacker_tool")
    assert "not in the allow-list" in res.lower()


def test_open_website_allowlisted():
    with patch("webbrowser.open") as mock_open:
        res = open_website("https://github.com/ponram06/kira")
        assert "opened website" in res.lower()
        mock_open.assert_called_once_with("https://github.com/ponram06/kira")


def test_open_website_disallowed():
    res = open_website("https://sketchy-untrusted-site.xyz")
    assert "not in the allow-list" in res.lower()


# ---------------------------------------------------------------------------
# Voice stub
# ---------------------------------------------------------------------------


def test_speak_stub(capsys: pytest.CaptureFixture):
    res = speak("Task completed successfully")
    captured = capsys.readouterr()
    assert "[Kira speaking]: Task completed successfully" in captured.out
    assert "Spoke:" in res
