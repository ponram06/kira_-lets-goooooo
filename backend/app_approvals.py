"""
app_approvals.py
================
Secure, persistent storage of first-launch user approvals for desktop applications
and Store apps.

Approvals are stored OUTSIDE the workspace and repository in %APPDATA%\\Kira\\app_approvals.json
(or %KIRA_USER_DIR%\\app_approvals.json).

Security Invariants:
1. Written atomically using a temp file and os.replace().
2. Not readable or writable by any LLM tool (filesystem tools restrict to workspace/).
3. Records target path, arguments, size, mtime, and SHA-256 checksum at approval time.
4. If target binary, size, modified time, or checksum changes, approval is invalidated.
5. Store apps key on validated AppID (strictly alphanumeric, dot, underscore, dash, and '!').
6. Only writable after an authenticated confirmation result, never via user_text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.user_config import user_dir

logger = logging.getLogger(__name__)

APPROVALS_FILE = "app_approvals.json"
_lock = threading.Lock()

# Store AppID format: e.g. SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify
_STORE_APPID_PATTERN = re.compile(r"^[A-Za-z0-9._\-]+![A-Za-z0-9._\-]+$")


def approvals_path() -> Path:
    """Return the absolute path to the app approvals JSON file outside workspace."""
    return user_dir(create=False) / APPROVALS_FILE


def is_valid_store_appid(app_id: str) -> bool:
    """
    Validate Store AppID characters.
    Must be alphanumeric, dots, underscores, dashes, with exactly one '!' separator.
    Rejects any shell characters, slashes, spaces, quotes, or semicolons.
    """
    if not app_id or not isinstance(app_id, str):
        return False
    return bool(_STORE_APPID_PATTERN.match(app_id.strip()))


def compute_file_sha256(path: Path | str) -> str:
    """Compute the SHA-256 hex digest of a local executable file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _make_key(target: str, arguments: str = "", is_store_app: bool = False) -> str:
    """Create normalized lookup key."""
    if is_store_app:
        return f"store:{target.strip()}"
    norm_path = str(Path(target).resolve()).lower()
    return f"file:{norm_path}|{arguments.strip()}"


def load_approvals() -> dict[str, dict[str, Any]]:
    """Load approvals from disk; returns {} if missing or corrupt."""
    path = approvals_path()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("App approvals file unreadable (%s); starting fresh.", exc)
        return {}


def is_target_approved(
    target: str,
    arguments: str = "",
    is_store_app: bool = False,
) -> bool:
    """
    Check if a target application and its arguments are approved and unmodified.

    For desktop applications:
    - Verifies file exists on disk.
    - Verifies current size and modified time match recorded values.
    - Verifies current SHA-256 matches the recorded SHA-256.

    For Store apps:
    - Validates AppID structure.
    - Verifies approval record exists.
    """
    if is_store_app:
        if not is_valid_store_appid(target):
            logger.warning("Rejected invalid Store AppID: %r", target)
            return False
        key = _make_key(target, is_store_app=True)
        approvals = load_approvals()
        return key in approvals

    # Desktop file target
    try:
        resolved = Path(target).resolve()
        if not resolved.is_file():
            return False

        key = _make_key(str(resolved), arguments, is_store_app=False)
        approvals = load_approvals()
        rec = approvals.get(key)
        if not rec or not isinstance(rec, dict):
            return False

        # Check size and mtime
        stat = resolved.stat()
        if stat.st_size != rec.get("size"):
            logger.info("Target size mismatch for %s (%d vs %d); approval invalid.", resolved, stat.st_size, rec.get("size"))
            return False

        # If mtime or sha256 changed, invalidate
        current_sha256 = compute_file_sha256(resolved)
        if current_sha256 != rec.get("sha256"):
            logger.info("Target SHA-256 checksum changed for %s; approval invalid.", resolved)
            return False

        return True
    except (OSError, ValueError) as exc:
        logger.debug("Failed checking target approval for %s: %s", target, exc)
        return False


def save_approval(
    target: str,
    arguments: str = "",
    is_store_app: bool = False,
) -> bool:
    """
    Atomically record an approval for a target application.
    Must only be called from an authorized confirmation path.
    """
    with _lock:
        approvals = load_approvals()

        if is_store_app:
            if not is_valid_store_appid(target):
                logger.error("Cannot approve invalid Store AppID: %r", target)
                return False
            key = _make_key(target, is_store_app=True)
            approvals[key] = {
                "type": "store",
                "app_id": target.strip(),
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            try:
                resolved = Path(target).resolve()
                if not resolved.is_file():
                    logger.error("Cannot approve non-existent target: %s", target)
                    return False

                stat = resolved.stat()
                sha256_hash = compute_file_sha256(resolved)
                key = _make_key(str(resolved), arguments, is_store_app=False)
                approvals[key] = {
                    "type": "file",
                    "target_path": str(resolved),
                    "arguments": arguments.strip(),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "sha256": sha256_hash,
                    "approved_at": datetime.now(timezone.utc).isoformat(),
                }
            except (OSError, ValueError) as exc:
                logger.error("Failed preparing approval record for %s: %s", target, exc)
                return False

        # Atomic write
        dir_path = user_dir(create=True)
        dest_path = dir_path / APPROVALS_FILE
        fd, tmp = tempfile.mkstemp(dir=str(dir_path), prefix=".approvals.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(approvals, f, indent=2)
            os.replace(tmp, dest_path)
            logger.info("Saved approval record for %s", key)
            return True
        except Exception as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            logger.error("Failed saving approval record: %s", exc)
            return False
