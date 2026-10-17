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


def rotate_log_file(log_path: Path, max_bytes: int = 5 * 1024 * 1024, backup_count: int = 3) -> None:
    """
    Rotate log_path if its size exceeds max_bytes, keeping up to backup_count backups.
    Fail-safe on OSError (e.g. locks or permissions) so application logging is not disrupted.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size < max_bytes:
            return
        for i in range(backup_count - 1, 0, -1):
            src = log_path.with_name(f"{log_path.stem}.{i}{log_path.suffix}")
            dst = log_path.with_name(f"{log_path.stem}.{i + 1}{log_path.suffix}")
            if src.exists():
                if dst.exists():
                    dst.unlink(missing_ok=True)
                src.rename(dst)
        dst1 = log_path.with_name(f"{log_path.stem}.1{log_path.suffix}")
        if dst1.exists():
            dst1.unlink(missing_ok=True)
        log_path.rename(dst1)
    except OSError:
        pass
