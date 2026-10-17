"""
config_loader.py
================
Single place that loads configuration:

    config.yaml (committed defaults)  +  user settings (backend.user_config)

Only the override paths listed in OVERRIDABLE can be changed by the user
settings file, each with a type check, so a bad settings.json can never
alter safety settings (allow-lists, server, policy, etc.).
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from backend import user_config

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path(__file__).parent.parent / "config.yaml"


def _short_text(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v) <= 128 and "\n" not in v


def _timeout(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 60 <= v <= 86400


def _device(v: Any) -> bool:
    return v is None or _short_text(v) or (isinstance(v, int) and not isinstance(v, bool) and v >= 0)


# Fixed (non-provider) override paths and their validators
OVERRIDABLE: dict[str, Callable[[Any], bool]] = {
    "voice.hotkey": _short_text,
    "voice.input_device": _device,
    "voice.tts.edge_voice": _short_text,
    "mode.free_revert_timeout": _timeout,
}


def is_overridable(path: str, value: Any) -> bool:
    """True if *path* may be set by the user to *value*."""
    if path.startswith("providers.") and path.endswith(".model"):
        return _short_text(value)
    check = OVERRIDABLE.get(path)
    return bool(check and check(value))


def _set_path(cfg: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            return
    node[parts[-1]] = value


def apply_overrides(cfg: dict, settings: dict) -> dict:
    """Return a copy of *cfg* with valid user overrides and persona applied."""
    merged = copy.deepcopy(cfg)
    for path, value in (settings.get("overrides") or {}).items():
        if not is_overridable(path, value):
            logger.debug("Ignoring user override for %s", path)
            continue
        if path.startswith("providers."):
            name = path[len("providers."):-len(".model")]
            for p in merged.get("providers", []):
                if p.get("name") == name:
                    p["model"] = value
        else:
            _set_path(merged, path, value)

    persona = settings.get("persona")
    available = merged.get("persona", {}).get("available_personas", {})
    if persona and persona in available:
        merged.setdefault("persona", {})["name"] = persona
    return merged


def load_yaml(path: Optional[Path] = None) -> dict:
    """Read config.yaml only (no user overrides)."""
    with open(path or DEFAULT_CONFIG, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: Optional[Path] = None, use_user_settings: bool = True) -> dict:
    """config.yaml merged with the user's saved settings."""
    cfg = load_yaml(path)
    if not use_user_settings:
        return cfg
    return apply_overrides(cfg, user_config.load_settings())
