"""
audit.py
========
Audit logger and live event feed for Kira actions.
Logs to logs/actions.jsonl and provides an emit(event) hook for future
avatar and WebSocket live feeds. Never logs keys or full user message text.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

LOGS_DIR = Path(__file__).parent.parent / "logs"
ACTIONS_LOG = LOGS_DIR / "actions.jsonl"

# Event listener callbacks (e.g. WebSocket feed)
_LISTENERS: list[Callable[[dict[str, Any]], None]] = []


def register_listener(callback: Callable[[dict[str, Any]], None]) -> None:
    """Register an event listener for live action feed."""
    if callback not in _LISTENERS:
        _LISTENERS.append(callback)


def remove_listener(callback: Callable[[dict[str, Any]], None]) -> None:
    """Remove a previously registered event listener."""
    if callback in _LISTENERS:
        _LISTENERS.remove(callback)


def emit(event: dict[str, Any]) -> None:
    """
    Broadcast action event to all registered listeners.
    Used for live action feed to the avatar and UI over WebSocket.
    """
    for listener in _LISTENERS:
        try:
            listener(event)
        except Exception as exc:
            logger.debug("Error in event listener: %s", exc)


def log_action(
    tool: str,
    args: dict[str, Any],
    decision: str,
    result: str,
) -> dict[str, Any]:
    """
    Log an executed tool action to logs/actions.jsonl and broadcast via emit().

    Parameters
    ----------
    tool:
        The tool name (e.g. 'run_command', 'create_folder').
    args:
        Sanitized tool arguments dictionary.
    decision:
        Policy decision ('allow', 'ask', 'countdown', 'block').
    result:
        Summary or snippet of the execution result.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Sanitize args to never include secrets
    sanitized_args = {k: v for k, v in args.items() if "key" not in k.lower() and "token" not in k.lower()}

    # Snippet of result
    result_snippet = result[:500] if isinstance(result, str) else str(result)

    event: dict[str, Any] = {
        "time": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "args": sanitized_args,
        "decision": decision,
        "result": result_snippet,
    }

    try:
        with open(ACTIONS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as exc:
        logger.warning("Failed to write to actions log: %s", exc)

    emit(event)
    return event
