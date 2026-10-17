"""
screen_tool.py
==============
M6 ``screen_task`` tool: wires backend.screen.controller to the real screen
(GDI screenshot, pyautogui input, foreground window lookup) and to the
vision provider from config.yaml. The agent loop awaits ``run_screen_task``
directly so every action can be confirmed; the plain ``screen_task`` entry in
TOOL_FUNCTIONS refuses (fail closed) if called any other way.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from backend.config_loader import load_config
from backend.policy import Mode, ModeManager
from backend.screen import capture, geometry
from backend.screen.controller import ScreenController, ScreenDeps

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"


def screen_settings(cfg: dict) -> dict:
    sc = {"enabled": True, "vision_provider": None, "max_actions": 10, "highlight_ms": 700,
          "max_image_width": 1280, "max_image_height": 800, "type_interval": 0.02}
    sc.update(cfg.get("screen_control") or {})
    return sc


def unavailable_reason(cfg: dict, active_provider: Optional[str], mode_manager: Optional[ModeManager]) -> Optional[str]:
    """Why screen control cannot run now (None = it can)."""
    from backend.screen.vision import resolve_provider
    sc = screen_settings(cfg)
    if not sc.get("enabled", True):
        return "Screen control is turned off in config.yaml (screen_control.enabled)."
    if (active_provider or "").lower() == "ollama":
        return "Screen control is disabled while the local Ollama model is answering."
    if mode_manager is not None and not mode_manager.screen_control_enabled:
        return "Screen control is disabled (Kira fell back to the local model). Enter Free mode again to re-enable it."
    if resolve_provider(cfg) is None:
        return ("Screen control is disabled: no vision provider is configured. "
                "Set screen_control.vision_provider in config.yaml to a cloud provider with a vision model.")
    return None


# ── real dependencies (not exercised by automated tests) ──────────────────

def _foreground() -> tuple[str, str]:
    import pygetwindow as gw
    from backend.tools.vscode_visible import _foreground_process_name
    return (gw.getActiveWindowTitle() or "", _foreground_process_name())


def _act(interval: float) -> Callable[[str, Optional[tuple[int, int]], dict], None]:
    def act(kind: str, point: Optional[tuple[int, int]], action: dict) -> None:
        import pyautogui
        from backend.policy import KILL_SWITCH
        if KILL_SWITCH.is_stopped():
            return
        if point is not None:
            pyautogui.moveTo(point[0], point[1], duration=0.15)
        if kind == "click":
            pyautogui.click()
        elif kind == "double_click":
            pyautogui.doubleClick()
        elif kind == "scroll":
            pyautogui.scroll(int(action.get("amount", 0)) * 120)
        elif kind == "key":
            keys = str(action.get("key", "")).lower().replace("escape", "esc").split("+")
            pyautogui.hotkey(*keys)
        elif kind == "type":
            text = str(action.get("text", ""))
            if point is not None:
                pyautogui.click()
            if text.isascii():
                pyautogui.write(text, interval=interval)
            else:
                import pyperclip
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
    return act


def _shooter(sc: dict) -> Callable[[], Any]:
    from backend.tools.system_control import _grab_screen, encode_png

    def shoot():
        return capture.take(geometry.virtual_desktop(), _grab_screen, encode_png,
                            int(sc["max_image_width"]), int(sc["max_image_height"]))
    return shoot


async def run_screen_task(
    goal: str,
    active_provider: Optional[str],
    mode_manager: ModeManager,
    confirm: Callable[[str], Any],
    speak: Optional[Callable[[str], Any]] = None,
    config_path: Path = _DEFAULT_CONFIG,
    deps: Optional[ScreenDeps] = None,
) -> str:
    """Run one screen-control task and return an honest report."""
    cfg = load_config(config_path)
    reason = unavailable_reason(cfg, active_provider, mode_manager)
    if reason:
        return f"Refused: {reason}"
    if not isinstance(goal, str) or not goal.strip():
        return "Error: screen_task needs a goal."
    sc = screen_settings(cfg)
    if deps is None:
        from backend.screen.vision import VisionClient, resolve_provider
        geometry.make_dpi_aware()
        client = VisionClient(resolve_provider(cfg))
        deps = ScreenDeps(
            propose=client.propose, shoot=_shooter(sc), monitors=geometry.monitors,
            foreground=_foreground, act=_act(float(sc["type_interval"])), confirm=confirm,
            get_mode=lambda: mode_manager.mode, speak=speak,
        )
    ctl = ScreenController(deps, max_actions=int(sc["max_actions"]), highlight_ms=int(sc["highlight_ms"]))
    result = await ctl.run(goal.strip()[:300])
    steps = "; ".join(result.actions[-10:]) or "no actions"
    return f"Screen control {result.status}: {result.message} (Actions: {steps})"


def screen_task(goal: str = "") -> str:
    """Direct calls are refused: screen control must run through the agent loop."""
    return "Refused: screen_task can only run through the agent loop (each action needs policy checks)."
