"""
brain.py
========
Chat loop with rolling conversation memory and unified voice/text controls.

Run from the project root directory::

    python -m backend.main              # terminal + voice (same as run.bat)
    python -m backend.main --headless   # started by the avatar app, no terminal input

Config comes from config.yaml merged with the user's saved settings
(backend.config_loader). After each reply, the name of the provider that
answered is printed in brackets and spoken out loud via TTS.

Free mode is only entered through the PIN dialog in the avatar menu (over the
token-authenticated WebSocket). It is never entered by typing or speaking, so
the PIN never passes through the terminal, the speech-to-text service or logs.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path
import sys
import threading
import time
from typing import Optional

# Reconfigure stdout to UTF-8 on Windows so emoji characters never crash the terminal
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.agent import AgentRunner
from backend import envfile
from backend import events as _events
from backend.app_state import AppState
from backend.config_loader import DEFAULT_CONFIG, load_config
from backend.policy import KILL_SWITCH, ModeManager
from backend.providers import ProviderManager
from backend.reminders import ReminderScheduler, register_scheduler
from backend.secrets_guard import PIN_ENV
from backend.server import WebSocketServer
from backend.tools.voice_stub import register_voice_manager
from backend import user_config
from backend.voice import VoiceManager, async_shared_confirm, list_input_devices

_DEFAULT_CONFIG = DEFAULT_CONFIG

RESTRICTED_PHRASES = (
    "restricted mode", "restricted", "go to restricted mode",
    "switch to restricted mode", "got to restricted mode",
)
FREE_PHRASES = (
    "free mode", "free", "go to free mode",
    "switch to free mode", "got to free mode",
)

FREE_HOWTO = "Free mode needs your PIN: open the avatar menu, choose Mode, then Free."
FREE_DISABLED = "Free mode is disabled because no PIN is set. Add one in Settings."

logger = logging.getLogger(__name__)


def build_mode_manager(cfg: dict) -> ModeManager:
    """ModeManager whose PIN is read fresh from .env (KIRA_FREE_MODE_PIN) on each attempt."""
    mode_cfg = cfg.get("mode", {})
    return ModeManager(
        free_timeout=int(mode_cfg.get("free_revert_timeout", 600)),
        pin_loader=lambda: envfile.get_value(PIN_ENV),
        max_attempts=int(mode_cfg.get("pin_max_attempts", 3)),
        lockout_seconds=float(mode_cfg.get("pin_lockout_seconds", 60)),
    )


def handle_local_command(text: str, mode_manager: ModeManager, voice_manager) -> Optional[str]:
    """
    Handle mode and kill-switch phrases locally (never sent to the LLM).
    Returns the reply text, or None if *text* is not a local command.
    """
    lower = text.lower().strip()
    if lower in RESTRICTED_PHRASES:
        mode_manager.enter_restricted()
        return "Switched to Restricted mode."
    if lower in FREE_PHRASES:
        return FREE_HOWTO if mode_manager.free_mode_available else FREE_DISABLED
    if lower == "stop":
        KILL_SWITCH.stop()
        voice_manager.stop_speech()
        return "Kill switch activated. All actions blocked."
    if lower in ("resume", "reset"):
        KILL_SWITCH.reset()
        return "Kill switch reset. Ready for actions."
    return None


async def chat_loop(config_path: Path = _DEFAULT_CONFIG, headless: bool = False) -> None:
    """
    Main async loop. Keeps the last *memory_limit* messages in a rolling
    deque and invokes AgentRunner with tools, policy enforcement, and TTS/STT.
    Exits on 'exit'/'quit' (terminal) or a WebSocket ``shutdown`` command.
    """
    cfg = load_config(config_path)
    memory_limit: int = cfg.get("memory_limit", 20)
    confirm_timeout: float = float(cfg.get("voice", {}).get("safety", {}).get("confirmation_timeout", 10.0))

    manager = ProviderManager(config_path)
    mode_manager = build_mode_manager(cfg)

    voice_manager = VoiceManager(cfg, mode_manager=mode_manager)
    register_voice_manager(voice_manager)

    state = AppState(cfg, voice_manager=voice_manager, mode_manager=mode_manager)
    if user_config.load_settings().get("mic_muted"):
        state.set_mic_muted(True, save=False)

    # Unified input queue for typed and spoken utterances
    user_queue: asyncio.Queue[tuple[str, str, Optional[float]]] = asyncio.Queue()

    async def confirm_cb(prompt_text: str) -> bool:
        return await async_shared_confirm(
            prompt_text,
            voice_manager=voice_manager,
            timeout_seconds=confirm_timeout,
            user_queue=user_queue,
        )

    agent = AgentRunner(
        provider_manager=manager,
        mode_manager=mode_manager,
        confirm_callback=confirm_cb,
        speak=voice_manager.speak,
    )

    # No microphone → the avatar offers the typed chat box instead
    state.mic_available = bool(list_input_devices())

    # Timers and reminders (survive restarts; announced by voice + on screen)
    async def on_reminder(item: dict) -> None:
        print(f"\n[{state.persona_display}]: {item['spoken']}")
        await voice_manager.speak(item["spoken"])

    scheduler = ReminderScheduler(on_due=on_reminder)
    register_scheduler(scheduler)

    history: deque[dict] = deque(maxlen=memory_limit)

    ws_server = WebSocketServer(
        config=cfg,
        config_path=config_path,
        mode_manager=mode_manager,
        voice_manager=voice_manager,
        app_state=state,
    )
    ws_server.start_in_thread()

    print("=" * 54)
    print(f"  {state.persona_display} — AI Dev Mentor")
    print(f"  Mode: {mode_manager.mode.value.upper()}" + ("" if headless else " | Type 'exit' or 'quit' to stop."))
    print("  Commands: 'restricted mode', 'stop', 'resume' (Free mode: avatar menu)")
    if not mode_manager.free_mode_available:
        print(f"  Free mode disabled: {PIN_ENV} is not set in .env")
    print(f"  Voice: Push-to-Talk [{voice_manager.ptt_hotkey_name}] (hold to talk)")
    print(f"  Kill: [{voice_manager.kill_hotkey_name}] | Diagnostic: python -m backend.doctor")
    if ws_server.running:
        print(f"  Feed: ws://{ws_server.host}:{ws_server.port}")
    print("=" * 54)
    voice_manager.print_privacy_notice()

    loop = asyncio.get_running_loop()
    state.bind_loop(loop)
    scheduler.start(loop)
    # Typed text from the avatar chat box joins the same queue as terminal input
    state.set_text_sink(lambda text: loop.call_soon_threadsafe(user_queue.put_nowait, ("typed", text, None)))
    voice_manager.start_hotkey_listener(loop)

    async def on_voice_utterance(utterance: str, speech_end_time: float) -> None:
        await user_queue.put(("voice", utterance, speech_end_time))

    voice_manager.on_utterance = on_voice_utterance

    def stdin_worker() -> None:
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    # In Windows console, empty read is not EOF; sleep briefly and continue
                    time.sleep(0.1)
                    continue
                text = line.strip()
                if text:
                    loop.call_soon_threadsafe(user_queue.put_nowait, ("typed", text, None))
            except Exception:
                time.sleep(0.1)

    if not headless:
        threading.Thread(target=stdin_worker, daemon=True).start()

    shutdown_task = asyncio.create_task(state.wait_shutdown())
    try:
        while True:
            if not headless:
                sys.stdout.write(f"\nYou [{mode_manager.mode.value}]: ")
                sys.stdout.flush()

            get_task = asyncio.create_task(user_queue.get())
            done, _ = await asyncio.wait({get_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
            if shutdown_task in done:
                get_task.cancel()
                print("\nShutdown requested. Goodbye.")
                break
            source, user_input, speech_end_time = get_task.result()
            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit"):
                print("Goodbye.")
                break

            local_reply = handle_local_command(user_input, mode_manager, voice_manager)
            if local_reply is not None:
                print(f"[{state.persona_display}]: {local_reply}")
                if not user_input.lower() == "stop":
                    await voice_manager.speak(local_reply)
                continue

            history.append({"role": "user", "content": user_input})
            messages: list[dict] = [
                {"role": "system", "content": state.system_prompt},
                *history,
            ]

            _events.publish(
                "voice.processing",
                f"{state.persona_display} is thinking...",
                {"user_input": user_input[:60]},
            )
            reply, provider = await agent.run(messages)
            history.append({"role": "assistant", "content": reply})

            tag: str = f"[{provider}]" if provider else "[unavailable]"
            print(f"\n{state.persona_display} {tag}:\n{reply}")
            _events.publish("assistant.reply", f"{state.persona_display} replied", {"text": reply[:1000]})

            await voice_manager.speak(reply, speech_end_time=speech_end_time)

    finally:
        shutdown_task.cancel()
        scheduler.stop()
        ws_server.stop()
        voice_manager.stop_hotkey_listener()
        voice_manager.stop_speech()


def main() -> None:
    from backend.main import main as _main
    _main([])


if __name__ == "__main__":
    main()
