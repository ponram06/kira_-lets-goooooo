"""
countdown.py
============
Cancellable Free-mode countdown (default 3 s) used before actions that
affect people (e.g. sending a message).

- Publishes ``policy.countdown`` once per second (avatar bubble + action feed).
- Optionally speaks the announcement first (``speak`` callback).
- Cancelled by the kill switch ("Stop" / hotkey) or by ``request_cancel()``
  (avatar Cancel button → WebSocket ``cancel_countdown``).
- Never blocks the event loop (asyncio.sleep in small slices).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, Callable, Optional

from backend import events as _events
from backend.policy import KILL_SWITCH

_cancel = threading.Event()


def request_cancel() -> None:
    """Cancel the countdown that is running now (no effect if none)."""
    _cancel.set()


async def run_countdown(
    label: str,
    seconds: int = 3,
    speak: Optional[Callable[[str], Awaitable[None]]] = None,
    slice_s: float = 0.1,
) -> bool:
    """Returns True if the countdown finished, False if it was cancelled."""
    _cancel.clear()
    announcement = f"{label} in {seconds} seconds. Say stop to cancel."
    _events.publish("policy.countdown", announcement, {"label": label[:80], "seconds_left": seconds})
    if speak is not None:
        try:
            await speak(announcement)
        except Exception:
            pass

    remaining = float(seconds)
    last_whole = seconds
    while remaining > 0:
        if KILL_SWITCH.is_stopped() or _cancel.is_set():
            _events.publish("policy.countdown", f"Cancelled: {label}", {"label": label[:80], "seconds_left": 0, "cancelled": True})
            return False
        await asyncio.sleep(slice_s)
        remaining -= slice_s
        whole = max(0, int(remaining + 0.999))
        if whole != last_whole and whole > 0:
            last_whole = whole
            _events.publish("policy.countdown", f"{label} in {whole}…", {"label": label[:80], "seconds_left": whole})
    if KILL_SWITCH.is_stopped() or _cancel.is_set():
        _events.publish("policy.countdown", f"Cancelled: {label}", {"label": label[:80], "seconds_left": 0, "cancelled": True})
        return False
    return True
