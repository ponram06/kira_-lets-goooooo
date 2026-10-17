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

- **ModeManager** — Restricted (default) / Free; PIN-gated Free mode with a
  wrong-PIN lockout; auto-revert timer; forces Restricted + disables screen control when the
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
import hmac
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
    - Starts in Restricted mode (the mode is never persisted).
    - ``enter_free(pin)`` needs a configured PIN (``KIRA_FREE_MODE_PIN`` in
      .env) and a matching *pin*. The UI's confirmation dialog is the
      deliberate confirmation; the PIN is the gate. No PIN configured →
      Free mode is disabled.
    - After *max_attempts* wrong PINs, every attempt is refused for
      *lockout_seconds*.
    - The PIN (right or wrong) is NEVER logged, printed or put in events.
    - ``enter_restricted()`` switches immediately (e.g. user says "restricted
      mode").
    - ``notify_provider(name)`` must be called after every LLM reply.  When
      *name* is ``"ollama"``, the mode is forced to Restricted and screen
      control is disabled.  This persists until the user deliberately calls
      ``enter_free()`` again.

    Parameters
    ----------
    free_timeout:
        Seconds before Free mode auto-reverts to Restricted.
    pin:
        Fixed PIN (tests). Ignored when *pin_loader* is given.
    pin_loader:
        Callable returning the current PIN (read fresh on every attempt so a
        PIN changed in Settings applies without a restart).
    max_attempts, lockout_seconds:
        Wrong-PIN lockout policy.
    clock:
        Injectable monotonic clock (tests).
    """

    # Results of try_enter_free()
    OK = "ok"
    DISABLED = "disabled"
    WRONG_PIN = "wrong_pin"
    LOCKED = "locked"

    def __init__(
        self,
        free_timeout: int = 600,
        pin: Optional[str] = None,
        pin_loader: Optional[Callable[[], Optional[str]]] = None,
        max_attempts: int = 3,
        lockout_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mode: Mode = Mode.RESTRICTED
        self._free_timeout: int = free_timeout
        self._fixed_pin: str = str(pin).strip() if pin else ""
        self._pin_loader = pin_loader
        self._max_attempts = max(1, int(max_attempts))
        self._lockout_seconds = float(lockout_seconds)
        self._clock = clock
        self._failed_attempts = 0
        self._locked_until = 0.0
        self._revert_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._screen_control_disabled: bool = False

    def __repr__(self) -> str:   # never expose the PIN in reprs / tracebacks
        return f"ModeManager(mode={self._mode.value})"

    # ── properties ──────────────────────────────────────────────────────────

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def screen_control_enabled(self) -> bool:
        """False while Ollama-forced Restricted mode is active."""
        return not self._screen_control_disabled

    # ── public methods ───────────────────────────────────────────────────────

    @property
    def free_timeout(self) -> int:
        return self._free_timeout

    @free_timeout.setter
    def free_timeout(self, seconds: int) -> None:
        self._free_timeout = max(60, int(seconds))

    def _current_pin(self) -> str:
        if self._pin_loader is not None:
            try:
                return str(self._pin_loader() or "").strip()
            except Exception:
                return ""
        return self._fixed_pin

    @property
    def free_mode_available(self) -> bool:
        """False when no PIN is configured (Free mode disabled)."""
        return bool(self._current_pin())

    def lockout_remaining(self) -> float:
        """Seconds left in the wrong-PIN lockout (0 when not locked)."""
        return max(0.0, self._locked_until - self._clock())

    def try_enter_free(self, pin: Optional[str]) -> str:
        """
        Attempt Free mode with *pin*. Returns one of OK, DISABLED, WRONG_PIN,
        LOCKED. Never logs or returns the PIN.
        """
        with self._lock:
            expected = self._current_pin()
            if not expected:
                logger.info("Free mode refused: no PIN configured.")
                self._publish_free_denied(self.DISABLED)
                return self.DISABLED
            if self.lockout_remaining() > 0:
                logger.info("Free mode refused: PIN entry locked out.")
                self._publish_free_denied(self.LOCKED)
                return self.LOCKED

            given = str(pin).strip() if pin is not None else ""
            if not given or not hmac.compare_digest(given.encode(), expected.encode()):
                self._failed_attempts += 1
                if self._failed_attempts >= self._max_attempts:
                    self._failed_attempts = 0
                    self._locked_until = self._clock() + self._lockout_seconds
                    logger.info("Free mode refused: wrong PIN, locked for %d s.", self._lockout_seconds)
                    self._publish_free_denied(self.LOCKED)
                    return self.LOCKED
                logger.info("Free mode refused: wrong PIN.")
                self._publish_free_denied(self.WRONG_PIN)
                return self.WRONG_PIN

            self._failed_attempts = 0
            self._mode = Mode.FREE
            self._screen_control_disabled = False   # re-enable on deliberate entry
            self._restart_revert_timer()
            logger.info(
                "Entered Free mode — auto-revert in %d s.", self._free_timeout
            )
            self._publish_mode_change(Mode.FREE, "user_confirmed")
            return self.OK

    def enter_free(self, pin: Optional[str] = None) -> bool:
        """Boolean wrapper around try_enter_free(): True if Free mode was entered."""
        return self.try_enter_free(pin) == self.OK

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

    def _publish_free_denied(self, reason: str) -> None:
        """Emit a free_mode.denied event (reason only — never the PIN)."""
        try:
            from backend import events as _events
            _events.publish(
                "free_mode.denied",
                f"Free mode refused ({reason.replace('_', ' ')})",
                {"reason": reason, "lockout_seconds": round(self.lockout_remaining())},
            )
        except Exception as exc:
            logger.debug("Failed to publish free_mode.denied event: %s", exc)

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

