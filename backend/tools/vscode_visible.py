"""
vscode_visible.py
=================
Visible VS Code automation tool for Kira.

Low-risk tool: operations strictly confined to workspace/.
Creates folder and empty file, opens VS Code on the folder, waits for the
target window to appear, brings it to foreground, types code visibly (or pastes
via clipboard for multi-line code to avoid auto-closing brackets and indentation
mangling), saves with Ctrl+S, and verifies the file on disk.

Safety & Guardrails
-------------------
- Only workspace/ paths are accepted (path escapes raise PolicyError).
- Uses keyboard shortcuts only, never screen coordinates.
- Aborts immediately if KILL_SWITCH is active or mouse hits a screen corner
  (pyautogui.FAILSAFE = True).
- Pauses push-to-talk while typing and restores it afterward.
- Verifies that the foreground window title matches VS Code and the target filename
  BEFORE every keystroke batch. If it does not, aborts immediately and types nothing.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

import pyautogui
import pygetwindow as gw
import pyperclip
import yaml

from backend import events as _events
from backend import policy as policy_module
from backend.policy import KILL_SWITCH, PolicyError, resolve_path
from backend.tools.filesystem import create_file, create_folder
from backend.tools.voice_stub import get_voice_manager

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).parent.parent.parent / "config.yaml"

# Enable pyautogui safety corner fail-safe (moving mouse to corner raises FailSafeException)
pyautogui.FAILSAFE = True


def _load_tool_config(config_path: Path = _DEFAULT_CONFIG) -> dict:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
            return cfg.get("tools", {})
    except Exception:
        return {}


def find_vscode_binary() -> str:
    """Find the full path or resolved command for VS Code on Windows / POSIX."""
    for candidate in ("code.cmd", "code.exe", "code"):
        found = shutil.which(candidate)
        if found:
            return found
    std_paths = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft VS Code\bin\code.cmd"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft VS Code\Code.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft VS Code\bin\code.cmd"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe"),
    ]
    for p in std_paths:
        if os.path.isfile(p):
            return p
    return "code.cmd" if os.name == "nt" else "code"


# Process names of VS Code builds whose window may receive keystrokes.
_VSCODE_PROCESSES = frozenset({"code.exe", "code - insiders.exe", "code", "vscodium.exe"})


def _foreground_process_name() -> str:
    """Lower-cased executable name of the foreground window's process ('' if unknown)."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        import ctypes.wintypes
        import psutil
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return psutil.Process(pid.value).name() if pid.value else ""
    except Exception as exc:
        logger.debug("Foreground process lookup failed: %s", exc)
        return ""


def is_vscode_window_active(filename: str) -> bool:
    """
    Verify that the foreground window is VS Code showing *filename*: the title
    must name the file and VS Code, AND (on Windows) the foreground process
    must be VS Code itself, so a browser tab titled "hello.py ... code" never
    receives keystrokes.
    """
    try:
        title = gw.getActiveWindowTitle()
        if not title:
            return False
        t_lower = title.lower()
        if filename.lower() not in t_lower:
            return False
        if "visual studio code" not in t_lower and "code" not in t_lower:
            return False
        if os.name == "nt":
            return _foreground_process_name().lower() in _VSCODE_PROCESSES
        return True
    except Exception as exc:
        logger.debug("Failed to query active window title: %s", exc)
        return False


def type_visibly(code: str, filename: str, interval: float, chunk: int = 8) -> tuple[bool, str]:
    """
    Type *code* in small chunks, re-checking before every chunk that VS Code
    still has focus and the kill switch is off. Stops at once otherwise, so at
    most one chunk can land in another window. Returns (ok, error message).
    """
    for i in range(0, len(code), max(1, chunk)):
        if KILL_SWITCH.is_stopped():
            return False, "Execution halted: Kill switch was activated."
        if not is_vscode_window_active(filename):
            return False, f"Error: Lost foreground focus on '{filename}' while typing. Keystrokes aborted."
        pyautogui.typewrite(code[i:i + chunk], interval=interval)
    return True, ""


def wait_for_vscode_window(
    filename: str,
    timeout: float = 15.0,
    poll_interval: float = 0.5,
) -> Optional[Any]:
    """
    Wait for a VS Code window containing *filename* in its title to appear.

    Returns the window object if found, or None if the timeout expires.
    """
    start_time = time.monotonic()
    f_lower = filename.lower()

    while time.monotonic() - start_time < timeout:
        if KILL_SWITCH.is_stopped():
            return None

        try:
            # Check all windows matching filename
            candidates = gw.getWindowsWithTitle(filename)
            for win in candidates:
                w_title = (win.title or "").lower()
                if f_lower in w_title and ("code" in w_title or "visual studio code" in w_title):
                    return win

            # Fallback: inspect all open windows
            for win in gw.getAllWindows():
                w_title = (win.title or "").lower()
                if f_lower in w_title and ("code" in w_title or "visual studio code" in w_title):
                    return win
        except Exception as exc:
            logger.debug("Error while querying windows: %s", exc)

        time.sleep(poll_interval)

    return None


def open_in_vscode(
    path: str,
    code: str,
    filename: Optional[str] = None,
    config_path: Path = _DEFAULT_CONFIG,
) -> str:
    """
    Open VS Code on a workspace folder, type code visibly, save, and verify.

    Parameters
    ----------
    path:
        Relative path inside workspace/ (e.g. 'demo' or 'demo/hello.py').
    code:
        Source code to type or paste into the file.
    filename:
        Optional filename (e.g. 'hello.py'). If omitted, derived from path.
    """
    # 1. Kill switch check before doing anything
    if KILL_SWITCH.is_stopped():
        return "Execution halted: Kill switch is active."

    # 2. Resolve target paths and boundaries
    raw_path_str = path.replace("\\", "/").strip()
    if filename:
        folder_rel = raw_path_str
        file_name = filename.strip()
        file_rel = f"{folder_rel}/{file_name}".strip("/")
    else:
        parts = raw_path_str.rsplit("/", 1)
        if len(parts) == 2 and "." in parts[1]:
            folder_rel = parts[0]
            file_name = parts[1]
            file_rel = raw_path_str
        else:
            folder_rel = raw_path_str
            file_name = "script.py"
            file_rel = f"{folder_rel}/{file_name}"

    try:
        resolved_folder = resolve_path(folder_rel)
        resolved_file = resolve_path(file_rel)
    except PolicyError as pe:
        return f"Error: Path escapes workspace: {pe}"

    # 3. Create folder and empty file
    create_folder(folder_rel)
    create_file(file_rel, "")

    cfg_tools = _load_tool_config(config_path)
    window_timeout = float(cfg_tools.get("vscode_window_timeout", 15))
    type_interval = float(cfg_tools.get("type_interval", 0.04))

    # 4. Launch VS Code with folder and file
    code_bin = find_vscode_binary()
    cmd = [code_bin, str(resolved_folder), str(resolved_file)]
    try:
        subprocess.Popen(cmd, shell=False)
    except Exception as exc:
        return f"Error: Failed to launch VS Code ('{code_bin}' command): {exc}"

    # 5. Wait for the real VS Code window to appear
    win = wait_for_vscode_window(file_name, timeout=window_timeout)
    if win is None:
        if KILL_SWITCH.is_stopped():
            return "Execution halted: Kill switch was activated while waiting for VS Code."
        err_msg = (
            f"Error: VS Code window for '{file_name}' did not appear within {window_timeout} seconds. "
            "Ensure VS Code is installed and 'code' is on system PATH."
        )
        _events.publish("tool.error", err_msg, {"filename": file_name, "timeout": window_timeout})
        return err_msg

    # 6. Focus and maximize window
    try:
        if hasattr(win, "activate"):
            win.activate()
        if hasattr(win, "maximize"):
            win.maximize()
    except Exception as exc:
        logger.debug("Could not maximize/activate window: %s", exc)

    time.sleep(0.6)

    # 7. Pause push-to-talk while typing
    vm = get_voice_manager()
    if vm is not None and hasattr(vm, "mute_mic"):
        vm.mute_mic(True)

    try:
        # Pre-keystroke security check: verify active window is target VS Code
        if not is_vscode_window_active(file_name):
            # Give one quick refocus attempt
            try:
                win.activate()
                time.sleep(0.4)
            except Exception:
                pass

        if not is_vscode_window_active(file_name):
            active_title = gw.getActiveWindowTitle() or "Unknown"
            err_msg = (
                f"Error: Target VS Code window for '{file_name}' is not in foreground "
                f"(active window: '{active_title}'). Keystrokes aborted."
            )
            _events.publish(
                "tool.error",
                err_msg,
                {"filename": file_name, "active_title": active_title},
            )
            return err_msg

        if KILL_SWITCH.is_stopped():
            return "Execution halted: Kill switch was activated."

        # 8. Type or paste code visibly
        is_multiline = "\n" in code or "\r" in code
        if is_multiline:
            # Use clipboard to prevent auto-indent and auto-closing bracket mangling
            prev_clipboard = ""
            try:
                prev_clipboard = pyperclip.paste()
            except Exception:
                pass

            try:
                pyperclip.copy(code)

                # Final verify active window immediately before Ctrl+V
                if not is_vscode_window_active(file_name):
                    err_msg = f"Error: Lost foreground focus on '{file_name}' before paste. Keystrokes aborted."
                    _events.publish("tool.error", err_msg, {"filename": file_name})
                    return err_msg

                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.2)
            finally:
                # Restore previous clipboard content
                try:
                    pyperclip.copy(prev_clipboard)
                except Exception:
                    pass
        else:
            # Single-line: visible typing, focus re-checked before every chunk
            ok, err_msg = type_visibly(code, file_name, type_interval)
            if not ok:
                _events.publish("tool.error", err_msg, {"filename": file_name})
                return err_msg
            time.sleep(0.1)

        # 9. Save with Ctrl+S
        if not is_vscode_window_active(file_name):
            err_msg = f"Error: Lost foreground focus on '{file_name}' before save. Save aborted."
            _events.publish("tool.error", err_msg, {"filename": file_name})
            return err_msg

        pyautogui.hotkey("ctrl", "s")
        time.sleep(0.4)

        # 10. Verify by reading file back from disk
        actual = resolved_file.read_text(encoding="utf-8")
        if actual.strip() != code.strip():
            # Retry save once on mismatch
            logger.info("Content mismatch on first read; retrying Ctrl+S save once.")
            if is_vscode_window_active(file_name):
                pyautogui.hotkey("ctrl", "s")
                time.sleep(0.6)
                actual = resolved_file.read_text(encoding="utf-8")

        if actual.strip() != code.strip():
            err_msg = (
                f"Error: Save verification failed for '{file_rel}'. "
                f"Expected {len(code.strip())} chars, found {len(actual.strip())}."
            )
            _events.publish("tool.error", err_msg, {"file": file_rel})
            return err_msg

        success_msg = f"Successfully typed code into VS Code and verified file at '{file_rel}' ({len(code)} chars)."
        return success_msg

    except pyautogui.FailSafeException:
        err_msg = "Execution aborted: PyAutoGUI fail-safe triggered (mouse moved to screen corner)."
        _events.publish("tool.error", err_msg, {})
        return err_msg
    except Exception as exc:
        err_msg = f"Error during VS Code automation: {exc}"
        _events.publish("tool.error", err_msg, {"error": str(exc)})
        return err_msg
    finally:
        # Resume push-to-talk
        if vm is not None and hasattr(vm, "mute_mic"):
            vm.mute_mic(False)
