"""
generic.py
==========
Generic application adapter leveraging AppIndex for universal launching.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.adapters.base import AdapterResult, AppAdapter
from backend.app_index import APP_INDEX

logger = logging.getLogger(__name__)


class GenericAppAdapter(AppAdapter):
    """Generic adapter capable of opening any indexed desktop or Store app."""

    name = "generic"
    aliases = ["app", "program"]
    actions = ["open"]

    def __init__(self, target_app_name: str = "") -> None:
        self.target_app_name = target_app_name

    def detect(self) -> bool:
        if not self.target_app_name:
            return True
        res = APP_INDEX.resolve(self.target_app_name)
        return res.status == "found"

    def launch(self) -> bool:
        if not self.target_app_name:
            return False
        res = self.execute("open", app_name=self.target_app_name)
        return res.success

    def execute(self, action: str, **params: Any) -> AdapterResult:
        if action not in self.actions:
            return AdapterResult(
                success=False,
                message=f"Unknown action '{action}' for generic adapter. Allowed actions: {self.actions}",
            )

        app_name = params.get("app_name") or self.target_app_name
        if not app_name:
            return AdapterResult(success=False, message="No application name specified.")

        # Defer execution to the secure launcher tool
        from backend.tools.app_launcher import open_any_app
        result = open_any_app(app_name)
        success = not result.lower().startswith("error") and not result.lower().startswith("action blocked")
        return AdapterResult(success=success, message=result)
