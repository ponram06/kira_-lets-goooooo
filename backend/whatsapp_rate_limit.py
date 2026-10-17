"""
whatsapp_rate_limit.py
======================
Persistent WhatsApp rate limiting and Free-mode contact approval management.

Storage:
- %APPDATA%\\Kira\\whatsapp_rate_limit.json (outside git)
- %APPDATA%\\Kira\\approved_whatsapp_contacts.json (outside git)

Security & Operational Invariants:
1. Written atomically using temp file and os.replace().
2. Corrupt or unreadable file blocks sending (fails closed, never resets counters).
3. Enforces minimum gap (30s), hourly cap (5), and daily cap (20).
4. Tracks first-contact approval in Free mode.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.user_config import user_dir

logger = logging.getLogger(__name__)

RATE_LIMIT_FILE = "whatsapp_rate_limit.json"
APPROVED_CONTACTS_FILE = "approved_whatsapp_contacts.json"

_lock = threading.Lock()


def rate_limit_path() -> Path:
    return user_dir(create=False) / RATE_LIMIT_FILE


def approved_contacts_path() -> Path:
    return user_dir(create=False) / APPROVED_CONTACTS_FILE


def _load_rate_limit_records() -> tuple[bool, list[float]]:
    """
    Load rate limit timestamps.
    Returns (ok, timestamps).
    If the file exists but cannot be parsed as valid JSON or valid timestamps list,
    returns (False, []) signaling a corrupted file (fail-closed).
    """
    p = rate_limit_path()
    if not p.is_file():
        return True, []

    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            logger.error("Rate limit file format invalid: root is not a dict")
            return False, []
        timestamps = data.get("attempts", [])
        if not isinstance(timestamps, list):
            logger.error("Rate limit file format invalid: 'attempts' is not a list")
            return False, []
        clean_ts = [float(t) for t in timestamps if isinstance(t, (int, float))]
        clean_ts.sort()
        return True, clean_ts
    except Exception as exc:
        logger.error("Rate limit file unreadable or corrupt (%s); failing closed.", exc)
        return False, []


def check_rate_limit(
    min_gap_seconds: float = 30.0,
    hourly_cap: int = 5,
    daily_cap: int = 20,
) -> tuple[bool, str]:
    """
    Check if a message can be sent under the active rate limits.
    Returns (allowed, reason_if_blocked).
    """
    with _lock:
        ok, timestamps = _load_rate_limit_records()
        if not ok:
            return False, "Rate limit data file is corrupt or unreadable. Blocking sending to fail closed."

        now = time.time()
        if timestamps:
            last_ts = timestamps[-1]
            elapsed = now - last_ts
            if elapsed < min_gap_seconds:
                wait_s = int(min_gap_seconds - elapsed) + 1
                return False, f"Rate limit: Minimum gap of {int(min_gap_seconds)}s not reached. Please wait {wait_s}s."

            in_last_hour = [t for t in timestamps if (now - t) < 3600.0]
            if len(in_last_hour) >= hourly_cap:
                return False, f"Rate limit: Hourly cap of {hourly_cap} messages reached. Please wait."

            in_last_day = [t for t in timestamps if (now - t) < 86400.0]
            if len(in_last_day) >= daily_cap:
                return False, f"Rate limit: Daily cap of {daily_cap} messages reached. Please wait."

        return True, ""


def record_send_attempt() -> bool:
    """
    Atomically record a send attempt timestamp.
    Prunes attempts older than 24 hours.
    """
    with _lock:
        ok, timestamps = _load_rate_limit_records()
        if not ok:
            logger.error("Cannot record send attempt: rate limit file corrupted.")
            return False

        now = time.time()
        # Keep only last 24h plus current
        pruned = [t for t in timestamps if (now - t) < 86400.0]
        pruned.append(now)

        dir_path = user_dir(create=True)
        dest = dir_path / RATE_LIMIT_FILE
        payload = {"attempts": pruned, "last_attempt": now}

        fd, tmp = tempfile.mkstemp(dir=str(dir_path), prefix=".ratelimit.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, dest)
            return True
        except Exception as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            logger.error("Failed atomically saving rate limit records: %s", exc)
            return False


def is_contact_approved_free_mode(contact_name: str) -> bool:
    """Check if contact was previously approved for Free-mode messaging."""
    p = approved_contacts_path()
    if not p.is_file():
        return False
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return contact_name.strip().lower() in data
    except Exception:
        pass
    return False


def approve_contact_free_mode(contact_name: str) -> bool:
    """Atomically record Free-mode contact approval."""
    with _lock:
        p = approved_contacts_path()
        data: dict[str, str] = {}
        if p.is_file():
            try:
                raw = p.read_text(encoding="utf-8")
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                pass

        data[contact_name.strip().lower()] = datetime.now(timezone.utc).isoformat()
        dir_path = user_dir(create=True)
        dest = dir_path / APPROVED_CONTACTS_FILE

        fd, tmp = tempfile.mkstemp(dir=str(dir_path), prefix=".approved_contacts.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, dest)
            return True
        except Exception as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            logger.error("Failed saving approved contact: %s", exc)
            return False
