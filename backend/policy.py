"""
policy.py
=========
Permission layer.  Every tool call must pass ``check()`` before it runs.

Key concepts
------------
- **RiskLevel** — LOW / MEDIUM / HIGH, declared per tool in TOOL_REGISTRY.
- **TOOL_REGISTRY** — maps tool name → metadata including ``allowlisted``,
  ``affects_people``, ``outside_workspace``, ``forbidden_category``.
- **check(action, mode)** — returns one of:
    ``"allow"``     proceed immediately
    ``"ask"``       pause and ask the user
    ``"countdown"`` start a 3-second cancellable timer, then run
    ``"block"``     refuse; explain to the user

- **ModeManager** — Restricted (default) / Free; injectable confirm callback;
  auto-revert timer; forces Restricted + disables screen control when the
  answering LLM provider is Ollama.

- **resolve_path(p)** — resolves *p* and raises ``PolicyError`` if it escapes
  ``WORKSPACE_ROOT`` (handles ``..``, absolute paths, mixed slashes, symlinks,
  and directory junctions).

- **KILL_SWITCH** — a global stop flag; when set, ``check()`` always blocks.

Limitation
----------
Short Windows 8.3 names (e.g. ``WORKSP~1``) may not be expanded by
``Path.resolve()`` on all filesystem configurations.  For full 8.3 coverage
add ``win32api.GetLongPathName()`` (requires *pywin32*).
"""

from __future__ import annotations

import enum
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Workspace root — the ONLY directory tools are allowed to modify.
# Tests can override this by patching ``policy.WORKSPACE_ROOT``.
# ---------------------------------------------------------------------------
WORKSPACE_ROOT: Path = (Path(__file__).parent.parent / "workspace").resolve()


# ---------------------------------------------------------------------------
# Risk levels
# ---------------------------------------------------------------------------
class RiskLevel(enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ---------------------------------------------------------------------------
# Permission modes
# ---------------------------------------------------------------------------
class Mode(enum.Enum):
    RESTRICTED = "restricted"
    FREE = "free"


# ---------------------------------------------------------------------------
# Forbidden categories — blocked in *every* mode regardless of anything else
# ---------------------------------------------------------------------------
FORBIDDEN_CATEGORIES: frozenset[str] = frozenset(
    [
        "disk_format",
        "system_security_settings",
        "payment",
        "type_password",
        "type_otp",
    ]
)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
# Each entry fields
# -----------------
# risk              RiskLevel
# allowlisted       bool  — may run without prompt when inside workspace/
# affects_people    bool  — involves sending content to another person
# outside_workspace bool  — the tool can touch paths outside workspace/
# forbidden_category str | None — maps to FORBIDDEN_CATEGORIES
# ---------------------------------------------------------------------------
TOOL_REGISTRY: dict[str, dict] = {
    # ── LOW risk — always allowed ────────────────────────────────────────────
    "open_app": {
        "risk": RiskLevel.LOW, "allowlisted": True,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    "open_site": {
        "risk": RiskLevel.LOW, "allowlisted": True,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    "open_website": {
        "risk": RiskLevel.LOW, "allowlisted": True,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    "create_folder": {
        "risk": RiskLevel.LOW, "allowlisted": True,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    "create_file": {
        "risk": RiskLevel.LOW, "allowlisted": True,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    "open_vscode": {
        "risk": RiskLevel.LOW, "allowlisted": True,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    "open_in_vscode": {
        "risk": RiskLevel.LOW, "allowlisted": True,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    "type_code": {
        "risk": RiskLevel.LOW, "allowlisted": True,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    "speak": {
        "risk": RiskLevel.LOW, "allowlisted": True,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },

    # ── MEDIUM risk ──────────────────────────────────────────────────────────
    # run_command / run_script: allowlisted + inside workspace → auto-allow
    "run_command": {
        "risk": RiskLevel.MEDIUM, "allowlisted": True,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    "run_script": {
        "risk": RiskLevel.MEDIUM, "allowlisted": True,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    # take_screenshot: special case — ask in Restricted, allow in Free
    "take_screenshot": {
        "risk": RiskLevel.MEDIUM, "allowlisted": False,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    # Reading a file outside workspace (not allowlisted → ask)
    "read_file_outside": {
        "risk": RiskLevel.MEDIUM, "allowlisted": False,
        "affects_people": False, "outside_workspace": True,
        "forbidden_category": None,
    },

    # ── HIGH risk ────────────────────────────────────────────────────────────
    # affects_people=True → countdown in Free mode
    "send_message": {
        "risk": RiskLevel.HIGH, "allowlisted": False,
        "affects_people": True, "outside_workspace": False,
        "forbidden_category": None,
    },
    "delete_file": {
        "risk": RiskLevel.HIGH, "allowlisted": False,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    "install_package": {
        "risk": RiskLevel.HIGH, "allowlisted": False,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": None,
    },
    "write_file_outside": {
        "risk": RiskLevel.HIGH, "allowlisted": False,
        "affects_people": False, "outside_workspace": True,
        "forbidden_category": None,
    },

    # ── Forbidden — blocked in every mode ───────────────────────────────────
    "format_disk": {
        "risk": RiskLevel.HIGH, "allowlisted": False,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": "disk_format",
    },
    "change_security_settings": {
        "risk": RiskLevel.HIGH, "allowlisted": False,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": "system_security_settings",
    },
    "make_payment": {
        "risk": RiskLevel.HIGH, "allowlisted": False,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": "payment",
    },
    "type_password": {
        "risk": RiskLevel.HIGH, "allowlisted": False,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": "type_password",
    },
    "type_otp": {
        "risk": RiskLevel.HIGH, "allowlisted": False,
        "affects_people": False, "outside_workspace": False,
        "forbidden_category": "type_otp",
    },
}


# ---------------------------------------------------------------------------
# Policy error
# ---------------------------------------------------------------------------
class PolicyError(Exception):
    """Raised when a path escapes the workspace or a policy rule is violated."""


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------
class _KillSwitch:
    """Global stop flag.  When set, check() always returns 'block'."""

    def __init__(self) -> None:
        self._flag = threading.Event()

    def stop(self) -> None:
        self._flag.set()
        logger.info("Kill switch activated — all actions blocked.")

    def reset(self) -> None:
        self._flag.clear()

    def is_stopped(self) -> bool:
        return self._flag.is_set()


KILL_SWITCH = _KillSwitch()


# ---------------------------------------------------------------------------
# Mode manager
# ---------------------------------------------------------------------------
class ModeManager:
    """
    Manages the permission mode (Restricted / Free).

    Rules
    -----
    - Starts in Restricted mode.
    - ``enter_free()`` calls the injectable *confirm_callback*; if it returns
      ``True``, switches to Free and starts the auto-revert timer.
    - ``enter_restricted()`` switches immediately (e.g. user says "restricted
      mode").
    - ``notify_provider(name)`` must be called after every LLM reply.  When
      *name* is ``"ollama"``, the mode is forced to Restricted and screen
      control is disabled.  This persists until the user deliberately calls
      ``enter_free()`` again.
    - The *confirm_callback* is injectable so a UI can plug in later.

    Parameters
    ----------
    free_timeout:
        Seconds before Free mode auto-reverts to Restricted.
    confirm_callback:
        Callable[[], bool] that returns True if the user approves Free mode.
        Defaults to a terminal prompt.
    """

    def __init__(
        self,
        free_timeout: int = 600,
        confirm_callback: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._mode: Mode = Mode.RESTRICTED
        self._free_timeout: int = free_timeout
        self._confirm_callback: Callable[[], bool] = (
            confirm_callback or self._terminal_confirm
        )
        self._revert_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._screen_control_disabled: bool = False

    # ── properties ──────────────────────────────────────────────────────────

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def screen_control_enabled(self) -> bool:
        """False while Ollama-forced Restricted mode is active."""
        return not self._screen_control_disabled

    # ── public methods ───────────────────────────────────────────────────────

    def enter_free(self) -> bool:
        """
        Attempt to switch to Free mode.  Calls the confirm callback.

        Returns ``True`` if Free mode was entered, ``False`` if denied.
        """
        with self._lock:
            if not self._confirm_callback():
                logger.info("Free mode: user declined confirmation.")
                return False
            self._mode = Mode.FREE
            self._screen_control_disabled = False   # re-enable on deliberate entry
            self._restart_revert_timer()
            logger.info(
                "Entered Free mode — auto-revert in %d s.", self._free_timeout
            )
            self._publish_mode_change(Mode.FREE, "user_confirmed")
            return True

    def enter_restricted(self) -> None:
        """Force Restricted mode immediately (e.g. user says 'restricted mode')."""
        with self._lock:
            self._cancel_timer()
            self._mode = Mode.RESTRICTED
            logger.info("Switched to Restricted mode.")
            self._publish_mode_change(Mode.RESTRICTED, "user_request")

    def notify_provider(self, provider_name: Optional[str]) -> None:
        """
        Call this after every LLM reply.

        If *provider_name* is ``"ollama"`` (case-insensitive):
        - Forces Restricted mode.
        - Disables screen control.
        - Cancels any pending Free-mode revert timer.
        The effect persists until ``enter_free()`` is called deliberately.
        """
        if provider_name and provider_name.lower() == "ollama":
            with self._lock:
                was_free = self._mode == Mode.FREE
                self._cancel_timer()
                self._mode = Mode.RESTRICTED
                self._screen_control_disabled = True
                if was_free:
                    logger.warning(
                        "Ollama is answering — forced Restricted mode, "
                        "screen control disabled."
                    )
                    self._publish_mode_change(Mode.RESTRICTED, "ollama_forced")

    # ── internal helpers ─────────────────────────────────────────────────────

    def _publish_mode_change(self, mode: Mode, reason: str) -> None:
        """Emit mode.change event to event bus without raising on error."""
        try:
            from backend import events as _events
            _events.publish(
                "mode.change",
                f"Mode changed to {mode.value.upper()} ({reason})",
                {"mode": mode.value, "reason": reason},
            )
        except Exception as exc:
            logger.debug("Failed to publish mode.change event: %s", exc)

    @staticmethod
    def _terminal_confirm() -> bool:
        """Default confirm: ask for 'yes' in the terminal."""
        try:
            answer = input(
                "Enable Free mode? Fewer confirmations will be asked. "
                "Type 'yes' to confirm: "
            )
            return answer.strip().lower() == "yes"
        except (EOFError, KeyboardInterrupt):
            return False

    def _restart_revert_timer(self) -> None:
        self._cancel_timer()
        self._revert_timer = threading.Timer(self._free_timeout, self._auto_revert)
        self._revert_timer.daemon = True
        self._revert_timer.start()

    def _cancel_timer(self) -> None:
        if self._revert_timer and self._revert_timer.is_alive():
            self._revert_timer.cancel()
        self._revert_timer = None

    def _auto_revert(self) -> None:
        with self._lock:
            self._mode = Mode.RESTRICTED
            logger.info("Free mode timed out — auto-reverted to Restricted.")
            self._publish_mode_change(Mode.RESTRICTED, "timeout")


# ---------------------------------------------------------------------------
# Workspace guard
# ---------------------------------------------------------------------------
def resolve_path(path: "str | Path") -> Path:
    """
    Resolve *path* and verify it lies within ``WORKSPACE_ROOT``.
    Relative paths are resolved relative to ``WORKSPACE_ROOT``.

    Raises ``PolicyError`` if the resolved path escapes the workspace.
    Handles: ``..`` traversal, absolute paths on other drives, mixed
    forward/back slashes, symlinks, and directory junctions (all resolved
    by ``Path.resolve()`` on Python 3.11+).

    Limitation: short 8.3 names (WORKSP~1) may not be expanded on all
    Windows filesystem configurations.
    """
    workspace: Path = WORKSPACE_ROOT  # dynamically uses active WORKSPACE_ROOT

    try:
        p = Path(path)
        if not p.is_absolute():
            resolved: Path = (workspace / p).resolve()
        else:
            resolved = p.resolve()
    except (OSError, ValueError) as exc:
        raise PolicyError(f"Invalid path: {exc}") from exc

    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise PolicyError(
            f"Path escapes workspace: {str(path)!r} resolved to {resolved}"
        )

    return resolved


# ---------------------------------------------------------------------------
# Main policy check
# ---------------------------------------------------------------------------
def check(
    action: str,
    mode: Mode,
    allowlisted: Optional[bool] = None,
    chained: bool = False,
) -> str:
    """
    Determine whether *action* is permitted in *mode*.

    Parameters
    ----------
    action:
        Tool name from TOOL_REGISTRY.
    mode:
        Current permission mode (RESTRICTED or FREE).
    allowlisted:
        Optional override for commands/apps: True if on allow-list, False if unknown.
    chained:
        If True (e.g. command contains shell metacharacters like & or |), immediately blocks.
    """
    # 0. Chained shell commands are always blocked
    if chained:
        return "block"

    # 1. Kill switch
    if KILL_SWITCH.is_stopped():
        return "block"

    entry: Optional[dict] = TOOL_REGISTRY.get(action)

    # Unknown action — treat conservatively
    if entry is None:
        return "block" if mode == Mode.RESTRICTED else "ask"

    # 2. Forbidden category — absolute block
    if entry.get("forbidden_category"):
        return "block"

    risk: RiskLevel = entry["risk"]
    if allowlisted is None:
        allowlisted = entry.get("allowlisted", False)
    affects_people: bool = entry.get("affects_people", False)
    outside_workspace: bool = entry.get("outside_workspace", False)

    # 3. Outside-workspace actions
    if outside_workspace:
        return "block" if mode == Mode.RESTRICTED else "ask"

    # 4. LOW risk
    if risk == RiskLevel.LOW:
        return "allow"

    # 5–7. MEDIUM risk
    if risk == RiskLevel.MEDIUM:
        # Special case: screenshots are privacy-sensitive even inside workspace
        if action == "take_screenshot":
            return "ask" if mode == Mode.RESTRICTED else "allow"
        # General MEDIUM: allow when allowlisted and inside workspace
        if allowlisted and not outside_workspace:
            return "allow"
        return "ask"

    # 8–10. HIGH risk
    if risk == RiskLevel.HIGH:
        if mode == Mode.RESTRICTED:
            # Always ask — never silently allow or block (unless kill/forbidden above)
            return "ask"
        # Free mode
        if affects_people:
            return "countdown"
        return "ask"

    # Fallback (should not be reached)
    return "ask"

