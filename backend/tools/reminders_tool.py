"""
reminders_tool.py
=================
Agent tools for timers and reminders (all LOW risk). They only talk to the
local scheduler in backend.reminders; nothing leaves the PC.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from backend import reminders as _r


@_r.as_tool_result
def set_timer(minutes: Optional[float] = None, seconds: Optional[float] = None, label: str = "") -> str:
    """Start a countdown timer (minutes and/or seconds, 1 s to 24 h)."""
    total = float(minutes or 0) * 60 + float(seconds or 0)
    if not 1 <= total <= 24 * 3600:
        raise ValueError("timer must be between 1 second and 24 hours")
    s = _r.ensure_scheduler()
    item = s.add(_r._now() + timedelta(seconds=total), label or "Timer", kind="timer")
    mins, secs = divmod(int(total), 60)
    span = f"{mins} min {secs} s" if mins and secs else (f"{mins} min" if mins else f"{secs} s")
    return f"Timer set for {span} (id {item['id']})."


@_r.as_tool_result
def set_reminder(when: str, text: str) -> str:
    """Remind at a local time: 'HH:MM' or 'YYYY-MM-DD HH:MM'."""
    due = _r.parse_when(when)
    if due <= _r._now():
        raise ValueError("that time has already passed")
    item = _r.ensure_scheduler().add(due, text, kind="reminder")
    return f"Reminder set: {_r.describe(item)}."


def list_reminders() -> str:
    """List pending timers and reminders."""
    items = _r.ensure_scheduler().list()
    if not items:
        return "No reminders or timers are set."
    return "Pending: " + "; ".join(_r.describe(i) for i in items)


def cancel_reminder(reminder_id: str) -> str:
    """Cancel a timer or reminder by its id."""
    if _r.ensure_scheduler().cancel(reminder_id):
        return f"Cancelled {reminder_id}."
    return f"Error: no reminder with id '{reminder_id}'."
