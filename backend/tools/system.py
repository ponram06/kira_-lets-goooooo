"""
system.py
=========
System tools for Kira: opening allow-listed applications and websites.
Strictly validates URLs (HTTPS only, exact domain or true subdomain matching)
and enforces workspace boundaries for explorer and code editors.
"""

from __future__ import annotations

import logging
import os
import subprocess
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

import yaml

from backend import policy as policy_module
from backend.policy import resolve_path

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).parent.parent.parent / "config.yaml"

DEFAULT_ALLOWED_APPS = ["code", "notepad", "calc", "cmd", "explorer"]
DEFAULT_ALLOWED_WEBSITES = ["github.com", "google.com", "python.org", "stackoverflow.com"]


def _get_allowed_apps(config_path: Path = _DEFAULT_CONFIG) -> list[str]:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
            return [
                a.lower()
                for a in cfg.get("tools", {}).get("allowed_apps", DEFAULT_ALLOWED_APPS)
            ]
    except Exception:
        return DEFAULT_ALLOWED_APPS


def _get_allowed_websites(config_path: Path = _DEFAULT_CONFIG) -> list[str]:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
            return [
                w.lower()
                for w in cfg.get("tools", {}).get("allowed_websites", DEFAULT_ALLOWED_WEBSITES)
            ]
    except Exception:
        return DEFAULT_ALLOWED_WEBSITES


def is_allowed_host(host: str, allowed_list: list[str]) -> bool:
    """
    Check if *host* is an exact match or a true subdomain of an allowed domain.
    Rejects lookalikes (e.g. 'notgithub.com' or 'github.com.evil.com').
    """
    clean_host = host.lower().strip()
    for allowed in allowed_list:
        clean_allowed = allowed.lower().strip()
        if clean_host == clean_allowed or clean_host.endswith("." + clean_allowed):
            return True
    return False


def open_app(
    app_name: str,
    path: Optional[str] = None,
    config_path: Path = _DEFAULT_CONFIG,
) -> str:
    """
    Open an allow-listed desktop application without a shell.
    For 'explorer' and 'code', any target path must reside inside workspace/.

    Parameters
    ----------
    app_name:
        Name of the app (e.g. 'code', 'notepad', 'calc', 'explorer').
    path:
        Optional path for code/explorer. Must reside inside workspace/.
    """
    clean_name = app_name.strip().lower()
    if clean_name.endswith(".exe"):
        clean_name = clean_name[:-4]

    allowed = _get_allowed_apps(config_path)
    if clean_name not in allowed:
        return f"Error: Application '{app_name}' is not in the allow-list."

    cmd: list[str] = [clean_name]

    # explorer and code: target path MUST be inside workspace/
    if clean_name in ("code", "explorer"):
        if path:
            target_path = resolve_path(path)
        else:
            target_path = policy_module.WORKSPACE_ROOT
        cmd.append(str(target_path))

    try:
        # Popen without shell
        subprocess.Popen(cmd, shell=False)
        return f"Application '{app_name}' launched successfully."
    except Exception as exc:
        return f"Failed to launch application '{app_name}': {exc}"


def open_website(url: str, config_path: Path = _DEFAULT_CONFIG) -> str:
    """
    Open an allow-listed website in the default browser.
    Only HTTPS URLs are allowed. Host must match exact domain or true subdomain.

    Parameters
    ----------
    url:
        URL to open (e.g. 'https://github.com/ponram06/kira').
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as exc:
        return f"Error: Invalid URL format: {exc}"

    # HTTPS only
    if parsed.scheme.lower() != "https":
        return "Error: Insecure URL rejected. Only HTTPS websites are permitted."

    host = (parsed.hostname or "").lower()
    if not host:
        return "Error: Could not extract hostname from URL."

    allowed = _get_allowed_websites(config_path)
    if not is_allowed_host(host, allowed):
        return f"Error: Host '{host}' is not in the allow-list."

    try:
        webbrowser.open(url)
        return f"Opened website: {url}"
    except Exception as exc:
        return f"Failed to open website '{url}': {exc}"
