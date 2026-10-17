"""
events.py
=========
In-process publish/subscribe event bus for Kira.

Every significant action (agent steps, tool start/finish, policy decisions,
mode changes, voice states) publishes a JSON event here.  Subscribers receive
events synchronously on the publishing thread (callbacks must be fast; offload
heavy work to threads or asyncio tasks).

Event schema
------------
{
    "type":  str,   # dot-namespaced, e.g. "tool.start"
    "ts":    str,   # ISO-8601 UTC timestamp
    "text":  str,   # <=120-char human-readable sentence (safe to display/speak)
    "data":  dict,  # optional structured payload — secrets stripped automatically
}

Event types published by Kira
------------------------------
  agent.step_start    Before each agent loop step
  agent.step_done     After each agent loop step (with result summary)
  tool.start          Before a tool call executes
  tool.done           After a tool call executes (duration, result snippet)
  policy.decision     After policy.check() returns (allow/ask/countdown/block)
  mode.change         When Restricted <-> Free mode switches
  voice.listening     PTT key pressed — microphone is open
  voice.processing    PTT released — STT is running
  voice.speaking      TTS playback starting
  voice.done          TTS playback complete or interrupted
  tool.error          A tool failed
  free_mode.denied    Free-mode request refused (reason only, never the PIN)
  persona.change      Persona switched live
  mic.mute            Microphone muted / unmuted
  sleep.change        Avatar sleep / wake
  app.shutdown        Backend shutdown requested

Rules
-----
- Never include secrets: any key whose name contains "key", "token", "secret",
  "password", "pin", "api_key" or "apikey" is stripped from *data* before
  delivery, and every known secret value (API keys, the Free-mode PIN) is
  replaced with [REDACTED] in *text* and *data* (backend.secrets_guard).
- JSONL lines are appended to logs/events.jsonl (which is in .gitignore).
- This module coexists with audit.py; neither replaces the other.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend import secrets_guard

logger = logging.getLogger(__name__)

LOGS_DIR = Path(__file__).parent.parent / "logs"
EVENTS_LOG = LOGS_DIR / "events.jsonl"

# Thread-safe subscriber list
_lock = threading.Lock()
_subscribers: list[Callable[[dict[str, Any]], None]] = []

# Field name fragments that identify secrets (case-insensitive match)
_SECRET_FRAGMENTS = frozenset({"key", "token", "secret", "password", "api_key", "apikey"})
# Exact field names that are secret but too short to match as fragments
_SECRET_NAMES = frozenset({"pin", "free_mode_pin"})


def _strip_secrets(data: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of *data* with secret-looking keys removed."""
    if not data:
        return {}
    return {
        k: v
        for k, v in data.items()
        if not any(frag in k.lower() for frag in _SECRET_FRAGMENTS)
        and k.lower() not in _SECRET_NAMES
    }


def subscribe(callback: Callable[[dict[str, Any]], None]) -> None:
    """
    Register *callback* to receive every event published via :func:`publish`.

    The callback is invoked synchronously on the publishing thread.
    It must be fast and must not raise exceptions.
    """
    with _lock:
        if callback not in _subscribers:
            _subscribers.append(callback)


def unsubscribe(callback: Callable[[dict[str, Any]], None]) -> None:
    """Remove *callback* so it no longer receives events."""
    with _lock:
        try:
            _subscribers.remove(callback)
        except ValueError:
            pass


def publish(
    event_type: str,
    text: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Publish an event to all subscribers and append it to logs/events.jsonl.

    Parameters
    ----------
    event_type:
        Dot-namespaced type string, e.g. ``"tool.start"``.
    text:
        Short human-readable description (truncated to 120 chars).
        Never include secrets here; this may be shown in a UI or spoken.
    data:
        Optional structured payload.  Secret-looking keys are stripped
        before the event is delivered or logged.

    Returns
    -------
    The event dict that was published (useful for testing).
    """
    safe_data = secrets_guard.redact_obj(_strip_secrets(data))

    event: dict[str, Any] = {
        "type": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
        "text": secrets_guard.redact(str(text))[:120],
        "data": safe_data,
    }

    # Deliver to subscribers — individual errors are logged, not re-raised
    with _lock:
        current = list(_subscribers)
    for cb in current:
        try:
            cb(event)
        except Exception as exc:
            logger.debug("Event subscriber raised an exception: %s", exc)

    # Persist to JSONL log
    _write_log(event)

    return event


def _write_log(event: dict[str, Any]) -> None:
    """Append *event* as one JSONL line to logs/events.jsonl."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.debug("Failed to write events log: %s", exc)
