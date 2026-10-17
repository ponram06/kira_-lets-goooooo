"""
paths.py
========
Writable locations, in one place (M10).

Development (default): everything lives in the project folder, as before:
workspace/, logs/, .env and .kira/token.

Packaged build: the code is read-only inside the install folder, so the
Electron app starts the bundled backend with ``KIRA_HOME`` (the user folder,
e.g. %APPDATA%\\Kira) and ``KIRA_WORKSPACE`` (e.g. Documents\\Kira Workspace).
Individual overrides: KIRA_WORKSPACE, KIRA_LOGS_DIR, KIRA_ENV_FILE.

Read-only files (config.yaml, prompts) still resolve next to the code, which
PyInstaller bundles alongside the modules.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def home() -> Path:
    """Base folder for writable data (project folder in development)."""
    h = os.environ.get("KIRA_HOME")
    return Path(h).expanduser().resolve() if h else SOURCE_ROOT


def workspace_root() -> Path:
    w = os.environ.get("KIRA_WORKSPACE")
    p = Path(w).expanduser() if w else home() / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def logs_dir() -> Path:
    d = os.environ.get("KIRA_LOGS_DIR")
    return Path(d).expanduser().resolve() if d else home() / "logs"


def env_file() -> Path:
    e = os.environ.get("KIRA_ENV_FILE")
    return Path(e).expanduser().resolve() if e else home() / ".env"
