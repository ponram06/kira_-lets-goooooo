"""
app_launcher.py
===============
Universal application launcher and media control tools for Kira.

Security & Policy Rules:
1. Model passes only 'name: str'. No paths, arguments, or shell commands from model.
2. Safe shell launching via os.startfile() or shell:AppsFolder\\<AppID>.
3. Whole-word denylist matching against prohibited tools and installers.
4. Refuses scripts (.bat, .cmd, .ps1, etc.) and interpreters (mshta, wscript, etc.).
5. Refuses administrator elevation / UAC prompts.
6. First-launch approval required in BOTH Restricted and Free modes.
7. Polls for window or process with timeout:
   - Returns "Opened {name}." if confirmed.
   - Returns "Command was sent to open {name}, but the window could not be confirmed." on timeout.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

import psutil

from backend.adapters import get_adapter
from backend.app_approvals import (
    is_target_approved,
    is_valid_store_appid,
    save_approval,
)
from backend.app_index import APP_INDEX, IndexedApp
from backend.app_security import (
    check_denylist,
    check_script_or_interpreter,
)
from backend.policy import KILL_SWITCH, Mode, check

logger = logging.getLogger(__name__)


def _poll_window_or_process(
    app: IndexedApp,
    timeout: float = 3.0,
    poll_interval: float = 0.2,
) -> bool:
    """
    Poll for the presence of the launched application window or active process.
    Returns True if confirmed within the timeout, False otherwise.
    """
    deadline = time.time() + timeout
    target_stem = Path(app.target_path).stem.lower() if app.target_path else ""
    app_name_lower = app.name.lower()

    # Import pygetwindow if available
    gw = None
    try:
        import pygetwindow as gw
    except Exception:
        pass

    while time.time() < deadline:
        # 1. Check window titles
        if gw:
            try:
                for win in gw.getAllWindows():
                    title = (win.title or "").lower()
                    if title and (app_name_lower in title or (target_stem and target_stem in title)):
                        return True
            except Exception:
                pass

        # 2. Check running processes
        try:
            for proc in psutil.process_iter(["name"]):
                try:
                    pname = (proc.info.get("name") or "").lower()
                    if target_stem and (pname == f"{target_stem}.exe" or pname == target_stem):
                        return True
                    if app_name_lower in pname:
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

        time.sleep(poll_interval)

    return False


def open_any_app(
    name: str,
    mode: Mode = Mode.RESTRICTED,
    confirm_cb: Optional[Callable[[str], bool]] = None,
    poll_timeout: float = 3.0,
) -> str:
    """
    Universal application launcher.

    Parameters
    ----------
    name:
        Application name (e.g. 'Spotify', 'Google Chrome').
    mode:
        Current permission mode (Mode.RESTRICTED or Mode.FREE).
    confirm_cb:
        Optional confirmation callback for first-launch approval.
        If None and unapproved, returns a confirmation requirement string.
    poll_timeout:
        Seconds to poll for window/process confirmation after launch.
    """
    clean_name = name.strip() if name else ""
    if not clean_name:
        return "Error: Application name cannot be empty."

    # 1. Kill switch check
    if KILL_SWITCH.is_stopped():
        return "Action blocked: kill switch is active."

    # 2. Resolve application via local AppIndex
    res = APP_INDEX.resolve(clean_name)
    if res.status == "not_found":
        return f"Error: {res.message}"
    if res.status == "weak_match":
        return f"Error: {res.message}"
    if res.status == "ambiguous":
        return f"Ambiguity: {res.message}"

    app = res.match
    if not app:
        return f"Error: Could not resolve application '{name}'."

    # 3. Security denylist check (whole-word matching, display name, Store AppID)
    blocked, reason = check_denylist(
        target_path=app.target_path,
        display_name=app.name,
        arguments=app.arguments,
        is_store_app=app.is_store_app,
        app_id=app.app_id,
    )
    if blocked:
        return f"Action blocked: {reason}"

    # 4. Refuse scripts and script interpreters (.bat, .cmd, mshta, etc.)
    if not app.is_store_app and app.target_path:
        refused, r_reason = check_script_or_interpreter(app.target_path)
        if refused:
            return f"Action blocked: {r_reason}"

    # 5. Check run-as-administrator flag (never trigger or type at UAC prompt)
    if app.run_as_admin:
        return (
            f"Action blocked: Application '{app.name}' requires administrator privileges "
            "(Run As Administrator flag set). Kira cannot elevate permissions or interact with UAC prompts."
        )

    # 6. Check Store AppID format
    if app.is_store_app and not is_valid_store_appid(app.app_id):
        return f"Action blocked: Store AppID '{app.app_id}' contains invalid characters."

    # 7. Check first-launch approval status
    if app.is_store_app:
        is_approved = is_target_approved(app.app_id, is_store_app=True)
    else:
        is_approved = is_target_approved(app.target_path, app.arguments, is_store_app=False)

    # 8. Check policy with approved flag (unapproved asks in BOTH modes)
    decision = check("open_any_app", mode, approved=is_approved)
    if decision == "block":
        return "Action blocked by safety policy."

    if decision == "ask":
        prompt = f"Kira would like to open '{app.name}' for the first time. Allow?"
        if confirm_cb:
            confirmed = confirm_cb(prompt)
            if not confirmed:
                return f"Action cancelled: user declined first-launch approval for '{app.name}'."
            # Record approval
            if app.is_store_app:
                save_approval(app.app_id, is_store_app=True)
            else:
                save_approval(app.target_path, app.arguments, is_store_app=False)
        else:
            return f"Confirmation required: {prompt}"

    # Re-check kill switch right before launch
    if KILL_SWITCH.is_stopped():
        return "Action blocked: kill switch is active."

    # 9. Launch application safely via Windows Shell
    try:
        if app.is_store_app:
            target_uri = f"shell:AppsFolder\\{app.app_id}"
            if os.name == "nt":
                os.startfile(target_uri)
            else:
                logger.info("Mock startfile(%s)", target_uri)
        else:
            launch_path = app.shortcut_path if (app.shortcut_path and os.path.exists(app.shortcut_path)) else app.target_path
            if os.name == "nt":
                os.startfile(launch_path)
            else:
                logger.info("Mock startfile(%s)", launch_path)
    except Exception as exc:
        return f"Error: Failed to launch application '{app.name}': {exc}"

    # 10. Poll for window or process with timeout
    confirmed = _poll_window_or_process(app, timeout=poll_timeout)
    if confirmed:
        return f"Opened {app.name}."
    else:
        return f"Command was sent to open {app.name}, but the window could not be confirmed."


def media_control(action: str, app: str = "spotify") -> str:
    """
    Execute keyboard media control on a supported application.

    Parameters
    ----------
    action:
        Media action ('open', 'play_pause', 'next', 'previous', 'volume_up', 'volume_down').
    app:
        Application name (default: 'spotify').
    """
    clean_action = (action or "").strip().lower()
    clean_app = (app or "spotify").strip().lower()

    # Kill switch check
    if KILL_SWITCH.is_stopped():
        return "Action blocked: kill switch is active."

    # Policy check for media_control
    decision = check("media_control", Mode.RESTRICTED)
    if decision == "block":
        return "Action blocked by safety policy."

    adapter = get_adapter(clean_app)
    if not adapter:
        return f"Error: No media adapter found for application '{app}'."

    res = adapter.execute(clean_action)
    if not res.success:
        return f"Error: {res.message}"
    return res.message

