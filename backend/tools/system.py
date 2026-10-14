"""
system.py
=========
System tools for Kira: opening allow-listed applications and websites.
"""

from __future__ import annotations

import logging
import os
import subprocess
import urllib.parse
import webbrowser
from pathlib import Path

import yaml

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


def open_app(app_name: str, config_path: Path = _DEFAULT_CONFIG) -> str:
    """
    Open an allow-listed desktop application.

    Parameters
    ----------
    app_name:
        Name of the app (e.g. 'code', 'notepad', 'calc').
    """
    clean_name = app_name.strip().lower()
    if clean_name.endswith(".exe"):
        clean_name = clean_name[:-4]

    allowed = _get_allowed_apps(config_path)
    if clean_name not in allowed:
        return f"Error: Application '{app_name}' is not in the allow-list."

    try:
        if os.name == "nt":
            subprocess.Popen([clean_name], shell=True)
        else:
            subprocess.Popen([clean_name])
        return f"Application '{app_name}' launched."
    except Exception as exc:
        return f"Failed to launch application '{app_name}': {exc}"


def open_website(url: str, config_path: Path = _DEFAULT_CONFIG) -> str:
    """
    Open an allow-listed website in the default browser.

    Parameters
    ----------
    url:
        URL to open (e.g. 'https://github.com').
    """
    parsed = urllib.parse.urlparse(url)
    domain = (parsed.netloc or parsed.path).lower()

    allowed = _get_allowed_websites(config_path)
    if not any(domain == a or domain.endswith("." + a) for a in allowed):
        return f"Error: Website domain '{domain}' is not in the allow-list."

    try:
        full_url = url if "://" in url else f"https://{url}"
        webbrowser.open(full_url)
        return f"Opened website: {full_url}"
    except Exception as exc:
        return f"Failed to open website '{url}': {exc}"
