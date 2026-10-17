"""
adapters/__init__.py
====================
Adapter registry and discovery for Kira application integrations.
"""

from __future__ import annotations

from typing import Optional

from backend.adapters.base import AdapterResult, AppAdapter
from backend.adapters.browser import BrowserAdapter
from backend.adapters.generic import GenericAppAdapter
from backend.adapters.spotify import SpotifyAdapter
from backend.adapters.whatsapp import WhatsAppAdapter

_ADAPTERS: dict[str, AppAdapter] = {
    "spotify": SpotifyAdapter(),
    "whatsapp": WhatsAppAdapter(),
    "browser": BrowserAdapter(),
    "generic": GenericAppAdapter(),
}


def get_adapter(name: str) -> Optional[AppAdapter]:
    """Look up an adapter by name or alias."""
    clean = name.strip().lower()
    if clean in _ADAPTERS:
        return _ADAPTERS[clean]

    for adapter in _ADAPTERS.values():
        if clean in adapter.aliases:
            return adapter

    return None


def list_adapters() -> list[str]:
    """Return names of all registered adapters."""
    return list(_ADAPTERS.keys())


__all__ = [
    "AdapterResult",
    "AppAdapter",
    "BrowserAdapter",
    "GenericAppAdapter",
    "SpotifyAdapter",
    "WhatsAppAdapter",
    "get_adapter",
    "list_adapters",
]

