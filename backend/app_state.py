"""
app_state.py
============
Live, user-controllable backend state shared by the chat loop, the voice
manager and the WebSocket server (tray / avatar menu commands).

- persona      switch live; saved to user settings
- mic mute     separate switch; saved to user settings
- sleep        avatar dims; push-to-talk still works and wakes it; NOT saved
- shutdown     clean exit of the chat loop (asyncio.Event, thread-safe)

Every change publishes an event so the avatar and action feed stay in sync.
The permission mode is deliberately NOT here: it lives in ModeManager and
always starts Restricted.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

from backend import events as _events
from backend import secrets_guard, user_config

logger = logging.getLogger(__name__)


class AppState:
    def __init__(
        self,
        cfg: dict,
        voice_manager: Any = None,
        mode_manager: Any = None,
        persist: bool = True,
    ) -> None:
        self._cfg = cfg
        self.voice_manager = voice_manager
        self.mode_manager = mode_manager
        self._persist = persist
        self._lock = threading.Lock()

        persona_cfg = cfg.get("persona", {})
        self._personas: dict = persona_cfg.get("available_personas", {}) or {}
        self._fallback_prompt: str = persona_cfg.get("system_prompt", "You are a helpful assistant.")
        self.persona: str = ""
        self._apply_persona(str(persona_cfg.get("name", "kira")).lower())

        self.sleeping: bool = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown: Optional[asyncio.Event] = None

        if voice_manager is not None:
            # Push-to-talk wakes the avatar from sleep (sleep never blocks the mic)
            voice_manager.on_ptt_press = self.wake_from_ptt

    # ── persona ──────────────────────────────────────────────────────────────

    @property
    def personas(self) -> list[str]:
        return list(self._personas.keys()) or [self.persona]

    @property
    def persona_display(self) -> str:
        return self._personas.get(self.persona, {}).get("name", self.persona).capitalize()

    @property
    def system_prompt(self) -> str:
        return self._personas.get(self.persona, {}).get("system_prompt", self._fallback_prompt)

    def _apply_persona(self, name: str) -> bool:
        if self._personas and name not in self._personas:
            return False
        self.persona = name
        return True

    def set_persona(self, name: str) -> bool:
        """Switch persona live. Unknown names are refused."""
        name = str(name or "").strip().lower()
        with self._lock:
            if not self._apply_persona(name):
                return False
        self._save(persona=name)
        _events.publish("persona.change", f"Persona: {self.persona_display}", {"persona": name})
        return True

    # ── microphone ───────────────────────────────────────────────────────────

    @property
    def mic_muted(self) -> bool:
        return bool(getattr(self.voice_manager, "is_muted_mic", False))

    def set_mic_muted(self, muted: bool, save: bool = True) -> None:
        muted = bool(muted)
        if self.voice_manager is not None:
            self.voice_manager.mute_mic(muted)
        if save:
            self._save(mic_muted=muted)
        _events.publish("mic.mute", "Microphone muted" if muted else "Microphone on", {"muted": muted})

    # ── sleep ────────────────────────────────────────────────────────────────

    def set_sleeping(self, sleeping: bool) -> None:
        sleeping = bool(sleeping)
        if sleeping == self.sleeping:
            return
        self.sleeping = sleeping
        _events.publish("sleep.change", "Sleeping" if sleeping else "Awake", {"sleeping": sleeping})

    def wake_from_ptt(self) -> None:
        """Called by VoiceManager when the push-to-talk key is pressed."""
        if self.sleeping:
            self.set_sleeping(False)

    # ── settings reload (after the Settings window saves) ───────────────────

    def reload_settings(self) -> None:
        """Apply settings that work live: persona, mic mute, Free-mode timeout."""
        secrets_guard.refresh()
        saved = user_config.load_settings()
        if saved.get("persona") and saved["persona"] != self.persona:
            self.set_persona(saved["persona"])
        if "mic_muted" in saved and saved["mic_muted"] != self.mic_muted:
            self.set_mic_muted(saved["mic_muted"], save=False)
        timeout = (saved.get("overrides") or {}).get("mode.free_revert_timeout")
        if self.mode_manager is not None and isinstance(timeout, int) and timeout >= 60:
            self.mode_manager.free_timeout = timeout

    # ── shutdown ─────────────────────────────────────────────────────────────

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to the chat loop's event loop (call from inside that loop)."""
        self._loop = loop
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        """Ask the chat loop to exit cleanly. Safe from any thread."""
        _events.publish("app.shutdown", "Shutting down", {})
        if self._loop is None or self._shutdown is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._shutdown.set)
        except RuntimeError:
            pass   # loop already closed

    async def wait_shutdown(self) -> None:
        if self._shutdown is None:
            raise RuntimeError("bind_loop() not called")
        await self._shutdown.wait()

    @property
    def shutdown_requested(self) -> bool:
        return bool(self._shutdown and self._shutdown.is_set())

    # ── status snapshot (no secrets) ─────────────────────────────────────────

    def status(self) -> dict:
        mm = self.mode_manager
        return {
            "persona": self.persona,
            "personas": self.personas,
            "mic_muted": self.mic_muted,
            "sleeping": self.sleeping,
            "free_mode_available": bool(mm and mm.free_mode_available),
            "free_timeout": int(mm.free_timeout) if mm else 0,
        }

    def _save(self, **changes: Any) -> None:
        if not self._persist:
            return
        try:
            user_config.update_settings(**changes)
        except OSError as exc:
            logger.warning("Could not save user settings: %s", type(exc).__name__)
