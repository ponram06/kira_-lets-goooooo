"""
controller.py
=============
M6 screen control loop (the last resort of the any-task planner):

    screenshot → vision model proposes ONE action → safety checks →
    highlight the target → policy (Restricted asks for every action) →
    act → next screenshot verifies the change → ... at most 10 actions.

Disabled when the answering provider is Ollama, when the mode manager has
screen control disabled, or when no vision provider is configured. The kill
switch is checked before every step and right before acting. Screenshots are
kept in memory only.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from backend import events as _events
from backend.policy import KILL_SWITCH, Mode, check
from backend.screen import safety
from backend.screen.vision import build_messages

logger = logging.getLogger(__name__)


@dataclass
class ScreenDeps:
    """Everything the loop touches, injectable for tests."""
    propose: Callable[[list[dict]], Awaitable[Optional[dict]]]   # vision call
    shoot: Callable[[], Any]                                      # → capture.Shot
    monitors: Callable[[], list]                                  # → [geometry.Rect]
    foreground: Callable[[], tuple[str, str]]                     # → (title, process name)
    act: Callable[[str, Optional[tuple[int, int]], dict], None]   # performs one action
    confirm: Callable[[str], Any]                                 # sync or async → bool
    get_mode: Callable[[], Mode]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    speak: Optional[Callable[[str], Awaitable[None]]] = None


@dataclass
class ScreenResult:
    status: str                  # done | failed | stopped | refused | declined | limit | halted
    message: str
    actions: list[str] = field(default_factory=list)


def describe(action: dict, point: Optional[tuple[int, int]]) -> str:
    kind = action.get("action")
    where = f" at ({point[0]}, {point[1]})" if point else ""
    if kind == "type":
        return f"type '{str(action.get('text', ''))[:80]}'" + (f" into {str(action.get('field', ''))[:40]}" if action.get("field") else "")
    if kind == "key":
        return f"press {str(action.get('key'))[:20]}"
    if kind == "scroll":
        return f"scroll {action.get('amount')}{where}"
    return f"{kind}{where}"


class ScreenController:
    def __init__(self, deps: ScreenDeps, max_actions: int = 10, highlight_ms: int = 700,
                 max_bad_proposals: int = 2) -> None:
        self.d = deps
        self.max_actions = max(1, int(max_actions))
        self.highlight_s = max(0, int(highlight_ms)) / 1000.0
        self.max_bad = max_bad_proposals

    async def _confirm(self, prompt: str) -> bool:
        res = self.d.confirm(prompt)
        if inspect.isawaitable(res):
            res = await res
        return bool(res)

    def _sensitive(self) -> Optional[str]:
        try:
            title, proc = self.d.foreground()
        except Exception:
            return "Could not identify the foreground window."
        v = safety.is_sensitive_window(title, proc)
        return None if v.ok else v.reason

    async def run(self, goal: str) -> ScreenResult:
        history: list[str] = []
        prev_digest: Optional[str] = None
        bad = 0
        _events.publish("screen.start", "Screen control started", {"max_actions": self.max_actions})
        for n in range(1, self.max_actions + 1):
            if KILL_SWITCH.is_stopped():
                return self._end("halted", "Screen control halted by the kill switch.", history)
            reason = self._sensitive()
            if reason:
                return self._end("refused", f"Stopped: {reason} I will not control this window.", history)

            shot = await asyncio.to_thread(self.d.shoot)
            changed = None if prev_digest is None else shot.digest != prev_digest
            try:
                action = await self.d.propose(build_messages(goal, history, changed, shot.data_url()))
            except Exception as exc:
                return self._end("failed", f"The vision model failed ({type(exc).__name__}).", history)
            if KILL_SWITCH.is_stopped():
                return self._end("halted", "Screen control halted by the kill switch.", history)

            verdict, point = safety.validate_action(action, shot.smap, self.d.monitors())
            if not verdict.ok:
                bad += 1
                history.append(f"REFUSED: {verdict.reason}")
                _events.publish("screen.refused", f"Refused screen action: {verdict.reason}", {"step": n})
                if action and action.get("action") == "type" and "type it yourself" in verdict.reason:
                    return self._end("refused", verdict.reason, history)
                if bad > self.max_bad:
                    return self._end("failed", f"Stopped after repeated invalid proposals. Last: {verdict.reason}", history)
                continue

            kind = action["action"]
            if kind == "done":
                return self._end("done", "The goal looks complete on screen: "
                                 + str(action.get("reason", ""))[:160], history)
            if kind == "fail":
                return self._end("failed", "I could not finish this on screen: "
                                 + str(action.get("reason", ""))[:160], history)

            desc = describe(action, point)
            decision = check("screen_action", self.d.get_mode())
            if decision == "block":
                return self._end("halted", "Screen action blocked by policy.", history)

            if point is not None:
                _events.publish("screen.highlight", f"Target: {desc}",
                                {"x": point[0], "y": point[1], "ms": int(self.highlight_s * 1000)})
                await self.d.sleep(self.highlight_s)

            if decision in ("ask", "countdown"):
                ok = await self._confirm(
                    f"Screen action {n}/{self.max_actions}: {desc}. "
                    f"(Model's reason, untrusted: {str(action.get('reason', ''))[:100]}) Allow? (yes/no):")
                if not ok:
                    return self._end("declined", f"You declined: {desc}.", history)

            # Last checks right before acting
            if KILL_SWITCH.is_stopped():
                return self._end("halted", "Screen control halted by the kill switch.", history)
            reason = self._sensitive()
            if reason:
                return self._end("refused", f"Stopped: {reason} I will not control this window.", history)

            try:
                await asyncio.to_thread(self.d.act, kind, point, action)
            except Exception as exc:
                history.append(f"{desc} -> ERROR {type(exc).__name__}")
                return self._end("failed", f"The action '{desc}' failed ({type(exc).__name__}).", history)
            history.append(f"{desc} -> done")
            _events.publish("screen.action", f"Screen: {desc}", {"step": n, "action": kind})
            prev_digest = shot.digest

        return self._end("limit", f"I used all {self.max_actions} screen actions without finishing. "
                         "Tell me if I should continue.", history)

    def _end(self, status: str, message: str, history: list[str]) -> ScreenResult:
        _events.publish("screen.end", f"Screen control {status}", {"status": status, "actions": len(history)})
        return ScreenResult(status, message, list(history))
