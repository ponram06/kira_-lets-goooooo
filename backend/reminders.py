"""
reminders.py
============
Local timers and reminders that survive a restart.

- Stored in the per-user folder (reminders.json, never in git).
- One asyncio task sleeps until the next due time (no polling loop); adding or
  cancelling wakes it to recompute.
- When due: publishes a ``reminder.due`` event (avatar bubble, action feed,
  Windows notification) and calls the ``on_due`` callback (spoken).
- Reminders that came due while Kira was off fire at the next start, marked
  as missed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from backend import events as _events
from backend import user_config

logger = logging.getLogger(__name__)

FILE_NAME = "reminders.json"
MAX_REMINDERS = 50
MAX_TEXT = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_when(text: str, now: Optional[datetime] = None) -> datetime:
    """
    Parse a local time: "HH:MM" (today, or tomorrow if already past),
    "YYYY-MM-DD HH:MM", or an ISO timestamp. Returns an aware UTC datetime.
    Raises ValueError for anything else.
    """
    s = str(text or "").strip()
    local_now = (now or _now()).astimezone()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            raise ValueError("time out of range")
        due = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if due <= local_now:
            due += timedelta(days=1)
        return due.astimezone(timezone.utc)
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1) if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}", s) else s)
    except ValueError:
        raise ValueError("use HH:MM or YYYY-MM-DD HH:MM") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()          # treat as local time
    return dt.astimezone(timezone.utc)


class ReminderScheduler:
    def __init__(
        self,
        path: Optional[Path] = None,
        on_due: Optional[Callable[[dict], Awaitable[None]]] = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._path = path
        self.on_due = on_due
        self._clock = clock
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._load()

    # ── storage ──────────────────────────────────────────────────────────────

    def _file(self) -> Path:
        return self._path or (user_config.user_dir() / FILE_NAME)

    def _load(self) -> None:
        try:
            data = json.loads(self._file().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("Reminders file unreadable; starting empty.")
            return
        for item in data if isinstance(data, list) else []:
            try:
                datetime.fromisoformat(item["due"])
                if isinstance(item.get("text"), str) and isinstance(item.get("id"), str):
                    self._items.append({"id": item["id"], "due": item["due"],
                                        "text": item["text"][:MAX_TEXT], "kind": item.get("kind", "reminder")})
            except (KeyError, TypeError, ValueError):
                continue

    def _save(self) -> None:
        path = self._file()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".reminders.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._items, f, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── public API (used by the tools) ───────────────────────────────────────

    def add(self, due: datetime, text: str, kind: str = "reminder") -> dict:
        text = " ".join(str(text or "").split())[:MAX_TEXT] or ("Timer" if kind == "timer" else "Reminder")
        with self._lock:
            if len(self._items) >= MAX_REMINDERS:
                raise ValueError(f"too many reminders (max {MAX_REMINDERS})")
            item = {"id": uuid.uuid4().hex[:6], "due": due.astimezone(timezone.utc).isoformat(),
                    "text": text, "kind": kind}
            self._items.append(item)
            self._items.sort(key=lambda i: i["due"])
            self._save()
        self._poke()
        return dict(item)

    def cancel(self, reminder_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i["id"] != str(reminder_id).strip()]
            changed = len(self._items) != before
            if changed:
                self._save()
        if changed:
            self._poke()
        return changed

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in sorted(self._items, key=lambda i: i["due"])]

    # ── scheduling ───────────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._wake = asyncio.Event()
        self._task = loop.create_task(self._run())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def _poke(self) -> None:
        if self._loop and self._wake:
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass

    def pop_due(self) -> list[dict]:
        """Remove and return reminders whose time has come."""
        now = self._clock()
        with self._lock:
            due = [i for i in self._items if datetime.fromisoformat(i["due"]) <= now]
            if due:
                ids = {i["id"] for i in due}
                self._items = [i for i in self._items if i["id"] not in ids]
                self._save()
        return due

    def seconds_until_next(self) -> Optional[float]:
        with self._lock:
            if not self._items:
                return None
            nxt = min(datetime.fromisoformat(i["due"]) for i in self._items)
        return max(0.0, (nxt - self._clock()).total_seconds())

    async def _run(self) -> None:
        while True:
            for item in self.pop_due():
                await self._fire(item)
            wait = self.seconds_until_next()
            self._wake.clear()
            try:
                # Cap the sleep so clock changes (sleep/hibernate) are noticed within 10 min
                await asyncio.wait_for(self._wake.wait(), timeout=min(wait, 600) if wait is not None else None)
            except asyncio.TimeoutError:
                pass

    async def _fire(self, item: dict) -> None:
        late = (self._clock() - datetime.fromisoformat(item["due"])).total_seconds()
        missed = late > 120
        label = "Timer done" if item["kind"] == "timer" else "Reminder"
        spoken = f"{label}: {item['text']}" + (" (missed while Kira was off)" if missed else "")
        _events.publish("reminder.due", spoken, {"id": item["id"], "text": item["text"],
                                                  "kind": item["kind"], "missed": missed})
        if self.on_due:
            try:
                await self.on_due({**item, "missed": missed, "spoken": spoken})
            except Exception as exc:
                logger.warning("Reminder callback failed: %s", type(exc).__name__)


# Module-level registration so tools can reach the running scheduler
_scheduler: Optional[ReminderScheduler] = None


def register_scheduler(s: Optional[ReminderScheduler]) -> None:
    global _scheduler
    _scheduler = s


def get_scheduler() -> Optional[ReminderScheduler]:
    return _scheduler


def describe(item: dict) -> str:
    local = datetime.fromisoformat(item["due"]).astimezone()
    return f"[{item['id']}] {local.strftime('%a %H:%M')} — {item['text']}"


def ensure_scheduler() -> ReminderScheduler:
    """Scheduler for tool calls; created lazily (not running) if Kira did not start one."""
    global _scheduler
    if _scheduler is None:
        _scheduler = ReminderScheduler()
    return _scheduler


def as_tool_result(fn: Callable[..., str]) -> Callable[..., str]:
    """Turn ValueError from parsing into an 'Error:' tool result."""
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            return f"Error: {exc}."
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper
