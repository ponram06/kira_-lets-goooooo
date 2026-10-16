"""
brain.py
========
Terminal chat loop with rolling conversation memory and unified voice/text controls.

Run from the project root directory::

    python -m backend.brain

The system prompt and memory size come from config.yaml so the persona can
be switched without touching code. After each reply, the name of the
provider that answered is printed in brackets and spoken out loud via TTS.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path
import sys
import threading
from typing import Optional

# Reconfigure stdout to UTF-8 on Windows so emoji characters never crash the terminal
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import yaml

from backend.agent import AgentRunner
from backend.policy import KILL_SWITCH, ModeManager
from backend.providers import ProviderManager
from backend.tools.voice_stub import register_voice_manager
from backend.voice import VoiceManager, shared_confirm

_DEFAULT_CONFIG = Path(__file__).parent.parent / "config.yaml"

# Only show WARNING+ from the library; lower levels are noisy in the terminal
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s  %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def chat_loop(config_path: Path = _DEFAULT_CONFIG) -> None:
    """
    Main async REPL. Keeps the last *memory_limit* messages in a rolling
    deque and invokes AgentRunner with tools, policy enforcement, and TTS/STT.
    """
    cfg = _load_config(config_path)

    persona: dict = cfg.get("persona", {})
    system_prompt: str = persona.get("system_prompt", "You are a helpful assistant.")
    memory_limit: int = cfg.get("memory_limit", 20)
    persona_name: str = persona.get("name", "Kira").capitalize()
    free_timeout: int = cfg.get("mode", {}).get("free_revert_timeout", 600)
    confirm_timeout: float = float(cfg.get("voice", {}).get("safety", {}).get("confirmation_timeout", 10.0))

    manager = ProviderManager(config_path)
    mode_manager = ModeManager(free_timeout=free_timeout)

    # Initialize Voice Manager
    voice_manager = VoiceManager(cfg, mode_manager=mode_manager)
    register_voice_manager(voice_manager)

    # Confirmation callback for agent using shared_confirm
    def confirm_cb(prompt_text: str) -> bool:
        return shared_confirm(prompt_text, voice_manager=voice_manager, timeout_seconds=confirm_timeout)

    agent = AgentRunner(
        provider_manager=manager,
        mode_manager=mode_manager,
        confirm_callback=confirm_cb,
    )

    # Rolling memory: user + assistant turns combined
    history: deque[dict] = deque(maxlen=memory_limit)

    print("=" * 54)
    print(f"  {persona_name} — AI Dev Mentor")
    print(f"  Mode: {mode_manager.mode.value.upper()} | Type 'exit' or 'quit' to stop.")
    print("  Commands: 'restricted mode', 'free mode', 'stop', 'resume'")
    print(f"  Voice: Push-to-Talk [{voice_manager.ptt_hotkey_name}] (hold to talk, or Fn+{voice_manager.ptt_hotkey_name} on laptops)")
    print(f"  Kill: [{voice_manager.kill_hotkey_name}] | Diagnostic: python -m backend.voice --test")
    print("=" * 54)
    voice_manager.print_privacy_notice()

    loop = asyncio.get_running_loop()
    voice_manager.start_hotkey_listener(loop)

    # Unified input queue for typed and spoken utterances
    user_queue: asyncio.Queue[tuple[str, str, Optional[float]]] = asyncio.Queue()

    async def on_voice_utterance(utterance: str, speech_end_time: float) -> None:
        await user_queue.put(("voice", utterance, speech_end_time))

    voice_manager.on_utterance = on_voice_utterance

    # Background reader thread for stdin
    def stdin_worker() -> None:
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    loop.call_soon_threadsafe(user_queue.put_nowait, ("typed", "exit", None))
                    break
                text = line.strip()
                if text:
                    loop.call_soon_threadsafe(user_queue.put_nowait, ("typed", text, None))
            except Exception:
                break

    stdin_thread = threading.Thread(target=stdin_worker, daemon=True)
    stdin_thread.start()

    try:
        while True:
            mode_indicator = f"[{mode_manager.mode.value}]"
            sys.stdout.write(f"\nYou {mode_indicator}: ")
            sys.stdout.flush()

            source, user_input, speech_end_time = await user_queue.get()

            if source == "voice":
                # The voice prompt already printed on its own line
                pass

            if not user_input:
                continue

            lower_input = user_input.lower()
            if lower_input in ("exit", "quit"):
                print("Goodbye.")
                break

            # Fast mode & kill-switch command handling
            if lower_input == "restricted mode":
                mode_manager.enter_restricted()
                print(f"[{persona_name}]: Switched to RESTRICTED mode.")
                await voice_manager.speak("Switched to Restricted mode.")
                continue
            elif lower_input == "free mode":
                if source == "voice":
                    msg = "Free mode cannot be enabled by voice. Please type 'free mode' in the terminal."
                    print(f"[{persona_name}]: {msg}")
                    await voice_manager.speak(msg)
                    continue
                if mode_manager.enter_free():
                    print(f"[{persona_name}]: Switched to FREE mode (auto-revert in {free_timeout}s).")
                else:
                    print(f"[{persona_name}]: Free mode declined. Remaining in Restricted mode.")
                continue
            elif lower_input == "stop":
                KILL_SWITCH.stop()
                voice_manager.stop_speech()
                print(f"[{persona_name}]: Kill switch ACTIVATED. All actions blocked.")
                continue
            elif lower_input in ("resume", "reset"):
                KILL_SWITCH.reset()
                print(f"[{persona_name}]: Kill switch RESET. Ready for actions.")
                continue

            # ---- build message list ----
            history.append({"role": "user", "content": user_input})
            messages: list[dict] = [
                {"role": "system", "content": system_prompt},
                *history,
            ]

            # ---- run agent loop ----
            reply, provider = await agent.run(messages)
            history.append({"role": "assistant", "content": reply})

            # ---- display reply ----
            tag: str = f"[{provider}]" if provider else "[unavailable]"
            print(f"\n{persona_name} {tag}:\n{reply}")

            # ---- voice narration ----
            await voice_manager.speak(reply, speech_end_time=speech_end_time)

    finally:
        voice_manager.stop_hotkey_listener()
        voice_manager.stop_speech()


def main() -> None:
    asyncio.run(chat_loop())


if __name__ == "__main__":
    main()
