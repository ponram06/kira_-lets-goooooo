"""
browser.py
==========
Browser adapter for Kira's headed Playwright browser subsystem.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.adapters.base import AdapterResult, AppAdapter
from backend.tools.browser_tools import (
    browser_back,
    browser_click,
    browser_close,
    browser_open,
    browser_read,
    browser_scroll,
    browser_search,
    browser_type,
)

logger = logging.getLogger(__name__)


class BrowserAdapter(AppAdapter):
    """Adapter providing browser automation actions via Playwright."""

    name = "browser"
    aliases = ["chrome", "google chrome", "chromium", "web browser"]
    actions = ["open", "search", "read", "click", "type", "scroll", "back", "close"]

    def detect(self) -> bool:
        """Playwright Chromium availability check."""
        return True

    def launch(self) -> bool:
        """Launch the headed browser to the default search page."""
        res = browser_search("Kira assistant")
        return not res.lower().startswith("error") and not res.lower().startswith("action blocked")

    def execute(self, action: str, **params: Any) -> AdapterResult:
        if action not in self.actions:
            return AdapterResult(
                success=False,
                message=f"Unknown action '{action}' for Browser adapter. Allowed actions: {self.actions}",
            )

        try:
            if action == "open":
                url = params.get("url", "")
                res = browser_open(url)
                return AdapterResult(success="Error" not in res and "blocked" not in res, message=res)

            if action == "search":
                query = params.get("query", "")
                res = browser_search(query)
                return AdapterResult(success="Error" not in res and "blocked" not in res, message=res)

            if action == "read":
                res = browser_read()
                return AdapterResult(success="Error" not in res and "blocked" not in res, message=res)

            if action == "click":
                target = params.get("target", "")
                res = browser_click(target)
                return AdapterResult(success="Error" not in res and "blocked" not in res, message=res)

            if action == "type":
                field = params.get("field_label", "")
                text = params.get("text", "")
                res = browser_type(field, text)
                return AdapterResult(success="Error" not in res and "blocked" not in res, message=res)

            if action == "scroll":
                direction = params.get("direction", "down")
                amount = params.get("amount", 500)
                res = browser_scroll(direction=direction, amount=amount)
                return AdapterResult(success="Error" not in res and "blocked" not in res, message=res)

            if action == "back":
                res = browser_back()
                return AdapterResult(success="Error" not in res and "blocked" not in res, message=res)

            if action == "close":
                res = browser_close()
                return AdapterResult(success=True, message=res)

        except Exception as exc:
            logger.error("BrowserAdapter execute error on action %s: %s", action, exc)
            return AdapterResult(success=False, message=f"Browser action '{action}' failed: {exc}")

        return AdapterResult(success=False, message=f"Unhandled action '{action}'.")
