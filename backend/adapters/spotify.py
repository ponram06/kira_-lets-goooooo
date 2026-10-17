"""
spotify.py
==========
Spotify desktop adapter: application launching and keyboard media controls.

Security & Operational Invariants:
- Fixed list of actions: ["open", "play_pause", "next", "previous", "volume_up", "volume_down"].
- Unknown actions are strictly rejected.
- Media keys confirm Spotify is running first before sending keystrokes.
- Reports "play/pause sent", never guessing or pretending "Spotify paused".
- Does NOT use spotify:search: URI or web API in M2B.
"""

from __future__ import annotations

import ctypes
import logging
import os
from typing import Any

import psutil

from backend.adapters.base import AdapterResult, AppAdapter
from backend.app_index import APP_INDEX

logger = logging.getLogger(__name__)

# Windows Virtual-Key Codes for Media Keys
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
KEYEVENTF_KEYUP = 0x0002


def _send_media_key(vk_code: int) -> bool:
    """Send a virtual media keystroke using Windows user32 keybd_event."""
    if os.name != "nt":
        return True
    try:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk_code, 0, 0, 0)
        user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception as exc:
        logger.error("Failed sending media key 0x%02X: %s", vk_code, exc)
        return False


class SpotifyAdapter(AppAdapter):
    """Adapter controlling Spotify desktop application and media playback."""

    name = "spotify"
    aliases = ["spotify music"]
    actions = ["open", "play_pause", "next", "previous", "volume_up", "volume_down"]

    def is_running(self) -> bool:
        """Check if Spotify.exe process is currently active."""
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info.get("name") or "").lower() == "spotify.exe":
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def detect(self) -> bool:
        """Detect whether Spotify is installed or currently running."""
        if self.is_running():
            return True
        res = APP_INDEX.resolve("spotify")
        return res.status == "found"

    def launch(self) -> bool:
        """Open the Spotify application."""
        from backend.tools.app_launcher import open_any_app
        res_str = open_any_app("spotify")
        return not res_str.lower().startswith("error") and not res_str.lower().startswith("action blocked")

    def execute(self, action: str, **params: Any) -> AdapterResult:
        if action not in self.actions:
            return AdapterResult(
                success=False,
                message=f"Unknown action '{action}' for Spotify adapter. Allowed actions: {self.actions}",
            )

        if action == "open":
            ok = self.launch()
            return AdapterResult(
                success=ok,
                message="Spotify launched successfully." if ok else "Failed launching Spotify.",
            )

        # Media playback actions require Spotify to be running
        if not self.is_running():
            return AdapterResult(
                success=False,
                message="Spotify is not currently running. Please open Spotify first.",
            )

        if action == "play_pause":
            _send_media_key(VK_MEDIA_PLAY_PAUSE)
            return AdapterResult(success=True, message="play/pause sent")

        if action == "next":
            _send_media_key(VK_MEDIA_NEXT_TRACK)
            return AdapterResult(success=True, message="next track sent")

        if action == "previous":
            _send_media_key(VK_MEDIA_PREV_TRACK)
            return AdapterResult(success=True, message="previous track sent")

        if action == "volume_up":
            _send_media_key(VK_VOLUME_UP)
            return AdapterResult(success=True, message="volume up sent")

        if action == "volume_down":
            _send_media_key(VK_VOLUME_DOWN)
            return AdapterResult(success=True, message="volume down sent")

        return AdapterResult(success=False, message=f"Unhandled action '{action}'.")

