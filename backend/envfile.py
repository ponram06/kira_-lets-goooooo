"""
envfile.py
==========
Small, dependency-free helpers for the local ``.env`` file.

- ``read_env(path)``        parse KEY=VALUE lines into a dict
- ``get_value(name)``       read one value: .env file first, then os.environ
- ``set_values(path, {..})`` update or add keys, keeping every other line
- ``mask_key(value)``       show only the last 4 characters of an API key

Rules: values are single-line; secrets are never logged or printed here.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

# Project-root .env (tests pass their own path or set KIRA_ENV_FILE)
_DEFAULT_ENV = Path(__file__).parent.parent / ".env"

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_VALUE_LEN = 512


def env_path() -> Path:
    """Return the active .env path (KIRA_ENV_FILE overrides the default)."""
    override = os.environ.get("KIRA_ENV_FILE")
    return Path(override) if override else _DEFAULT_ENV


def _parse_line(line: str) -> Optional[tuple[str, str]]:
    """Return (name, value) for a KEY=VALUE line, or None for comments/blank."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    name, _, value = stripped.partition("=")
    name = name.strip()
    if name.startswith("export "):
        name = name[len("export "):].strip()
    value = value.strip()
    # Remove one pair of matching surrounding quotes
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return name, value


def read_env(path: Optional[Path] = None) -> dict[str, str]:
    """Parse the .env file. Missing file → empty dict."""
    p = path or env_path()
    result: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed:
            result[parsed[0]] = parsed[1]
    return result


def get_value(name: str, path: Optional[Path] = None) -> str:
    """Current value of *name*: the .env file wins (so edits apply live), then os.environ."""
    file_val = read_env(path).get(name)
    if file_val:
        return file_val
    return os.environ.get(name, "") or ""


def validate_value(value: str) -> None:
    """Raise ValueError if *value* cannot be stored safely on one .env line."""
    if not isinstance(value, str):
        raise ValueError("value must be text")
    if len(value) > MAX_VALUE_LEN:
        raise ValueError("value is too long")
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise ValueError("value must be a single line")
    if value != value.strip():
        raise ValueError("value must not start or end with spaces")


def set_values(updates: dict[str, str], path: Optional[Path] = None) -> None:
    """
    Update or add *updates* in the .env file, keeping all other lines and
    comments. An empty string removes the key. Written atomically.
    """
    p = path or env_path()
    for name, value in updates.items():
        if not _NAME_RE.match(name):
            raise ValueError("invalid variable name")
        validate_value(value)

    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed and parsed[0] in remaining:
            value = remaining.pop(parsed[0])
            if value:
                out.append(f"{parsed[0]}={value}")
            # empty value → drop the line (key removed)
            continue
        out.append(line)
    for name, value in remaining.items():
        if value:
            out.append(f"{name}={value}")

    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mask_key(value: str) -> str:
    """'sk-abcdef1234' → '••••1234'. Short or empty values reveal nothing."""
    if not value:
        return ""
    if len(value) < 12:
        return "••••"
    return "••••" + value[-4:]
