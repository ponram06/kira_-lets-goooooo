"""
user_config.py
==============
Per-user settings stored OUTSIDE the repository (never committed).

Folder: %KIRA_USER_DIR% if set, else %APPDATA%\\Kira, else ~/.kira-user.
File:   settings.json, written atomically by the backend only.

Saved keys (everything else is dropped on save):
    persona     "kira" | "ryuk" | ...
    mic_muted   bool
    overrides   {dotted.config.path: value} applied over config.yaml by
                backend.config_loader (only whitelisted paths are honoured)

The permission mode is NEVER saved: Kira always starts in Restricted mode.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SETTINGS_FILE = "settings.json"
_ALLOWED_KEYS = ("persona", "mic_muted", "overrides")
_lock = threading.Lock()


def user_dir(create: bool = False) -> Path:
    """Resolve the per-user config folder (created only when *create* is True)."""
    override = os.environ.get("KIRA_USER_DIR")
    if override:
        base = Path(override)
    elif os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"]) / "Kira"
    else:
        base = Path.home() / ".kira-user"
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return base


def settings_path() -> Path:
    return user_dir() / SETTINGS_FILE


def _sanitize(data: Any) -> dict:
    """Keep only allowed keys with the right types."""
    if not isinstance(data, dict):
        return {}
    clean: dict[str, Any] = {}
    persona = data.get("persona")
    if isinstance(persona, str) and persona.strip():
        clean["persona"] = persona.strip().lower()[:32]
    if isinstance(data.get("mic_muted"), bool):
        clean["mic_muted"] = data["mic_muted"]
    overrides = data.get("overrides")
    if isinstance(overrides, dict):
        clean["overrides"] = {
            str(k): v for k, v in overrides.items()
            if isinstance(v, (str, int, float, bool)) or v is None
        }
    return clean


def load_settings() -> dict:
    """Read settings.json; a missing or corrupt file gives {}."""
    try:
        raw = settings_path().read_text(encoding="utf-8")
        return _sanitize(json.loads(raw))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("User settings unreadable (%s); using defaults.", type(exc).__name__)
        return {}


def save_settings(data: dict) -> dict:
    """Atomically write sanitized *data*; returns what was stored."""
    clean = _sanitize(data)
    path = user_dir(create=True) / SETTINGS_FILE
    with _lock:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(clean, f, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return clean


def update_settings(**changes: Any) -> dict:
    """Merge *changes* into the stored settings (``overrides`` is merged key by key)."""
    current = load_settings()
    for key, value in changes.items():
        if key not in _ALLOWED_KEYS:
            continue
        if key == "overrides" and isinstance(value, dict):
            merged = dict(current.get("overrides", {}))
            merged.update(value)
            current["overrides"] = merged
        else:
            current[key] = value
    return save_settings(current)
