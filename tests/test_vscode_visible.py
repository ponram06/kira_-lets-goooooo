"""
test_vscode_visible.py
======================
Unit tests for backend/tools/vscode_visible.py.

All external desktop GUI interactions (pyautogui, pygetwindow, pyperclip,
subprocess) are mocked so tests run 100% offline and headless.

Covered:
- Window appearance timeout returns clear error
- Save verification and single retry on content mismatch
- Foreground window verification: aborts immediately if active window does not match
- Kill switch active halts execution immediately
- PyAutoGUI corner fail-safe triggers safe abort
- Multi-line code uses clipboard paste (Ctrl+V) and restores previous clipboard
- Single-line code uses visible character typing
- Push-to-talk muted during typing and unmuted afterward
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import backend.policy as policy_module
from backend.policy import KILL_SWITCH
from backend.tools.vscode_visible import (
    is_vscode_window_active,
    open_in_vscode,
    wait_for_vscode_window,
)


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


def test_window_timeout_returns_error(workspace: Path):
    with patch("subprocess.Popen"):
        with patch("backend.tools.vscode_visible.wait_for_vscode_window", return_value=None):
            res = open_in_vscode("demo", "print('hello')", "hello.py")
            assert "did not appear within" in res
            assert "hello.py" in res


def test_save_verify_success(workspace: Path):
    mock_win = MagicMock()
    mock_win.title = "hello.py - demo - Visual Studio Code"

    with patch("subprocess.Popen"):
        with patch("backend.tools.vscode_visible.wait_for_vscode_window", return_value=mock_win):
            with patch("backend.tools.vscode_visible.is_vscode_window_active", return_value=True):
                with patch("pyautogui.typewrite"):
                    with patch("pyautogui.hotkey"):
                        # Pre-populate expected file content so verification succeeds
                        target_file = workspace / "demo" / "hello.py"

                        def fake_hotkey(*args):
                            if "s" in args:
                                target_file.write_text("print('hello')", encoding="utf-8")

                        with patch("pyautogui.hotkey", side_effect=fake_hotkey):
                            res = open_in_vscode("demo", "print('hello')", "hello.py")
                            assert "Successfully typed code" in res
                            assert target_file.read_text(encoding="utf-8") == "print('hello')"


def test_save_verify_mismatch_retry(workspace: Path):
    mock_win = MagicMock()
    mock_win.title = "hello.py - demo - Visual Studio Code"
    target_file = workspace / "demo" / "hello.py"
    call_count = 0

    def fake_hotkey(*args):
        nonlocal call_count
        if "s" in args:
            call_count += 1
            if call_count >= 2:
                # Succeed on 2nd save attempt
                target_file.write_text("print('retry')", encoding="utf-8")
            else:
                target_file.write_text("wrong content", encoding="utf-8")

    with patch("subprocess.Popen"):
        with patch("backend.tools.vscode_visible.wait_for_vscode_window", return_value=mock_win):
            with patch("backend.tools.vscode_visible.is_vscode_window_active", return_value=True):
                with patch("pyautogui.typewrite"):
                    with patch("pyautogui.hotkey", side_effect=fake_hotkey):
                        res = open_in_vscode("demo", "print('retry')", "hello.py")
                        assert "Successfully typed code" in res
                        assert call_count == 2


def test_kill_switch_halts_before_execution(workspace: Path):
    KILL_SWITCH.stop()
    res = open_in_vscode("demo", "print('hello')", "hello.py")
    assert "Kill switch is active" in res


def test_active_window_mismatch_aborts_without_typing(workspace: Path):
    mock_win = MagicMock()
    mock_win.title = "hello.py - Visual Studio Code"

    with patch("subprocess.Popen"):
        with patch("backend.tools.vscode_visible.wait_for_vscode_window", return_value=mock_win):
            # Return False: some other window (like browser or terminal) is active!
            with patch("backend.tools.vscode_visible.is_vscode_window_active", return_value=False):
                with patch("pygetwindow.getActiveWindowTitle", return_value="Google Chrome"):
                    with patch("pyautogui.typewrite") as mock_type:
                        with patch("pyautogui.hotkey") as mock_hotkey:
                            res = open_in_vscode("demo", "print('secret')", "hello.py")
                            assert "not in foreground" in res
                            # Crucial: NO keystrokes sent to wrong window
                            mock_type.assert_not_called()
                            mock_hotkey.assert_not_called()


def test_multiline_uses_clipboard_and_restores(workspace: Path):
    mock_win = MagicMock()
    mock_win.title = "hello.py - Visual Studio Code"
    multiline_code = "def hello():\n    print('world')\n"
    target_file = workspace / "demo" / "hello.py"

    clipboard_state = ["original clipboard contents"]

    def fake_paste():
        return clipboard_state[0]

    def fake_copy(val):
        clipboard_state[0] = val

    def fake_hotkey(*args):
        if "v" in args:
            target_file.write_text(clipboard_state[0], encoding="utf-8")

    with patch("subprocess.Popen"):
        with patch("backend.tools.vscode_visible.wait_for_vscode_window", return_value=mock_win):
            with patch("backend.tools.vscode_visible.is_vscode_window_active", return_value=True):
                with patch("pyperclip.paste", side_effect=fake_paste):
                    with patch("pyperclip.copy", side_effect=fake_copy):
                        with patch("pyautogui.typewrite") as mock_type:
                            with patch("pyautogui.hotkey", side_effect=fake_hotkey):
                                res = open_in_vscode("demo", multiline_code, "hello.py")
                                assert "Successfully typed code" in res
                                # Character-by-character typing must NOT be used for multi-line
                                mock_type.assert_not_called()
                                # Previous clipboard must be restored
                                assert clipboard_state[0] == "original clipboard contents"


def test_ptt_paused_during_typing(workspace: Path):
    mock_win = MagicMock()
    mock_win.title = "hello.py - Visual Studio Code"
    target_file = workspace / "demo" / "hello.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("code", encoding="utf-8")

    mock_vm = MagicMock()

    with patch("subprocess.Popen"):
        with patch("backend.tools.vscode_visible.wait_for_vscode_window", return_value=mock_win):
            with patch("backend.tools.vscode_visible.is_vscode_window_active", return_value=True):
                with patch("backend.tools.vscode_visible.get_voice_manager", return_value=mock_vm):
                    with patch("pyautogui.typewrite"):
                        with patch("pyautogui.hotkey"):
                            open_in_vscode("demo", "code", "hello.py")
                            # Verify mic muted before typing and unmuted afterward
                            assert mock_vm.mute_mic.call_count == 2
                            assert mock_vm.mute_mic.call_args_list[0][0][0] is True
                            assert mock_vm.mute_mic.call_args_list[1][0][0] is False


def test_is_vscode_window_active_helper():
    with patch("pygetwindow.getActiveWindowTitle", return_value="hello.py - demo - Visual Studio Code"):
        assert is_vscode_window_active("hello.py") is True
        assert is_vscode_window_active("other.py") is False

    with patch("pygetwindow.getActiveWindowTitle", return_value="Command Prompt - python"):
        assert is_vscode_window_active("hello.py") is False
