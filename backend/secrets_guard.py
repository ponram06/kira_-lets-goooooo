"""
secrets_guard.py
================
Last line of defence against secrets leaking into logs, events, stdout or
WebSocket payloads.

Known secret *values* are every value in the local .env file (it only holds
secrets) plus any environment variable named like an API key or the Free-mode
PIN. ``redact(text)`` replaces each known value with ``[REDACTED]``.

Wire-up (done once at startup by backend.main):
    install_log_redaction()        → logging filter on the root logger
    RedactingStream(sys.stdout)    → wraps stdout/stderr in headless mode
events.publish() and server._broadcast() call redact() directly.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, TextIO

from backend import envfile

PIN_ENV = "KIRA_FREE_MODE_PIN"
REDACTED = "[REDACTED]"
_MIN_LEN = 4                       # shorter values would redact ordinary text
_SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
_REFRESH_SECONDS = 2.0

_lock = threading.Lock()
_cache: list[str] = []
_cache_stamp: tuple[float, float] = (-1.0, 0.0)   # (.env mtime, time checked)


def _collect() -> list[str]:
    """Gather all known secret values, longest first so overlaps redact fully."""
    values: set[str] = set()
    values.update(v for v in envfile.read_env().values() if v)
    for name, val in os.environ.items():
        if val and (name == PIN_ENV or name.upper().endswith(_SECRET_SUFFIXES)):
            values.add(val)
    return sorted((v for v in values if len(v) >= _MIN_LEN), key=len, reverse=True)


def known_secrets() -> list[str]:
    """Cached list of secret values; refreshed when .env changes (checked every 2 s)."""
    global _cache, _cache_stamp
    now = time.monotonic()
    with _lock:
        mtime_seen, checked_at = _cache_stamp
        if now - checked_at < _REFRESH_SECONDS and checked_at > 0:
            return _cache
        try:
            mtime = envfile.env_path().stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime != mtime_seen or checked_at == 0:
            _cache = _collect()
        _cache_stamp = (mtime, now)
        return _cache


def refresh() -> None:
    """Force a reload of known secrets (e.g. right after Settings saved .env)."""
    global _cache_stamp
    with _lock:
        _cache_stamp = (-1.0, 0.0)


def redact(text: Any) -> Any:
    """Replace known secret values inside *text*. Non-strings pass through."""
    if not isinstance(text, str) or not text:
        return text
    for secret in known_secrets():
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside dicts, lists and tuples."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact_obj(v) for v in obj)
    return obj


class RedactingFilter(logging.Filter):
    """Logging filter that renders the message and strips secrets from it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        clean = redact(message)
        if clean != message:
            record.msg = clean
            record.args = None
        # Tracebacks: pre-render and redact (Formatter reuses exc_text if set)
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(_FORMATTER.formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


_FORMATTER = logging.Formatter()


def install_log_redaction() -> None:
    """Attach RedactingFilter to the root logger and all its handlers (idempotent)."""
    root = logging.getLogger()
    targets = [root, *root.handlers]
    for target in targets:
        if not any(isinstance(f, RedactingFilter) for f in target.filters):
            target.addFilter(RedactingFilter())


class RedactingStream:
    """Text stream wrapper (stdout/stderr) that redacts secrets on write."""

    def __init__(self, inner: TextIO) -> None:
        self._inner = inner

    def write(self, data: str) -> int:
        self._inner.write(redact(data))
        return len(data)

    def flush(self) -> None:
        self._inner.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
