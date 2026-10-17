"""
whatsapp.py
===========
WhatsApp adapter for Kira.

Security & Operational Invariants:
- In Milestone 2B, WhatsApp adapter ONLY opens the application.
- Does NOT register or use whatsapp:// links.
- Messaging actions are strictly rejected/deferred to Milestone 3.
- Fixed action list: ["open"]. Unknown actions are rejected.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.adapters.base import AdapterResult, AppAdapter
from backend.app_index import APP_INDEX

logger = logging.getLogger(__name__)


class WhatsAppAdapter(AppAdapter):
    """Adapter for WhatsApp desktop / Store application."""

    name = "whatsapp"
    aliases = ["whatsapp desktop"]
    actions = ["open", "send_message"]

    def detect(self) -> bool:
        """Detect whether WhatsApp desktop or Store app is installed."""
        res = APP_INDEX.resolve("whatsapp")
        return res.status == "found"

    def launch(self) -> bool:
        """Open the WhatsApp application."""
        from backend.tools.app_launcher import open_any_app
        res_str = open_any_app("whatsapp")
        return not res_str.lower().startswith("error") and not res_str.lower().startswith("action blocked")

    def execute(self, action: str, **params: Any) -> AdapterResult:
        if action not in self.actions:
            return AdapterResult(
                success=False,
                message=f"Unknown action '{action}' for WhatsApp adapter. Allowed actions: {self.actions}",
            )

        if action == "open":
            ok = self.launch()
            return AdapterResult(
                success=ok,
                message="WhatsApp opened successfully." if ok else "Failed opening WhatsApp.",
            )

        if action == "send_message":
            from backend.tools.whatsapp_tool import send_whatsapp
            contact = params.get("contact", "")
            text = params.get("text", "")
            request_id = params.get("request_id")
            res_str = send_whatsapp(contact=contact, text=text, request_id=request_id)
            success = "I pressed Enter" in res_str
            return AdapterResult(success=success, message=res_str)

        return AdapterResult(success=False, message=f"Unhandled action '{action}'.")

