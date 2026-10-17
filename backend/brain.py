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
import time
from typing import Optional

# Reconfigure stdout to UTF-8 on Windows so emoji characters never crash the terminal
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import yaml

from backend.agent import AgentRunner
from backend import events as _events
from backend.policy import KILL_SWITCH, ModeManager
from backend.providers import ProviderManager
from backend.server import WebSocketServer
from backend.tools.voice_stub import register_voice_manager
from backend.voice import VoiceManager, async_shared_confirm, shared_confirm

_DEFAULT_CONFIG = Path(__file__).parent.parent / "config.yaml"

# Spoken number word mapping for PIN entry
_PIN_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "sixteen": "16", "eighty": "80", "four": "4", "eighty-four": "84",
}


def extract_pin_digits(text: str) -> str:
    """Extract digits from typed or spoken PIN string."""
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        return digits
    normalized = text.lower().strip()
    compounds = {
        "sixteen eighty-four": "1684",
        "sixteen eighty four": "1684",
        "eighty-four": "84",
        "eighty four": "84",
        "sixteen": "16",
    }
    for phrase, num_str in compounds.items():
        if phrase in normalized:
            normalized = normalized.replace(phrase, num_str)

    digits = "".join(ch for ch in normalized if ch.isdigit())
    if digits:
        return digits

    words = normalized.replace("-", " ").split()
    converted = "".join(_PIN_NUMBER_WORDS.get(w, "") for w in words)
    return converted or text.strip()

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
    p_name = persona.get("name", "kira").lower()
    avail = persona.get("available_personas", {})
    if p_name in avail:
        persona_name = avail[p_name].get("name", p_name).capitalize()
        system_prompt = avail[p_name].get("system_prompt", persona.get("system_prompt", ""))
    else:
        persona_name = persona.get("name", "Kira").capitalize()
        system_prompt = persona.get("system_prompt", "You are a helpful assistant.")
    memory_limit: int = cfg.get("memory_limit", 20)
    free_timeout: int = cfg.get("mode", {}).get("free_revert_timeout", 600)
    free_mode_pin: str = str(cfg.get("mode", {}).get("free_mode_pin", "1684")).strip()
    confirm_timeout: float = float(cfg.get("voice", {}).get("safety", {}).get("confirmation_timeout", 10.0))

    manager = ProviderManager(config_path)
    mode_manager = ModeManager(free_timeout=free_timeout, pin=free_mode_pin)

    # Initialize Voice Manager
    voice_manager = VoiceManager(cfg, mode_manager=mode_manager)
    register_voice_manager(voice_manager)

    # Unified input queue for typed and spoken utterances
    user_queue: asyncio.Queue[tuple[str, str, Optional[float]]] = asyncio.Queue()

    # Non-blocking async confirmation callback using async_shared_confirm
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
    )

    # Rolling memory: user + assistant turns combined
    history: deque[dict] = deque(maxlen=memory_limit)

    # Initialize WebSocket Server (Step 6)
    ws_server = WebSocketServer(
        config=cfg,
        config_path=config_path,
        mode_manager=mode_manager,
        voice_manager=voice_manager,
    )
    ws_server.start_in_thread()

    print("=" * 54)
    print(f"  {persona_name} — AI Dev Mentor")
    print(f"  Mode: {mode_manager.mode.value.upper()} | Type 'exit' or 'quit' to stop.")
    print("  Commands: 'restricted mode', 'free mode', 'stop', 'resume'")
    fn_hint = f", or Fn+{voice_manager.ptt_hotkey_name}" if voice_manager.ptt_hotkey_name.lower().startswith("f") else ""
    print(f"  Voice: Push-to-Talk [{voice_manager.ptt_hotkey_name}] (hold to talk{fn_hint})")
    print(f"  Kill: [{voice_manager.kill_hotkey_name}] | Diagnostic: python -m backend.voice --test")
    if ws_server.running:
        print(f"  Feed: ws://{ws_server.host}:{ws_server.port} (token: {ws_server.token_file})")
    print("=" * 54)
    voice_manager.print_privacy_notice()

    loop = asyncio.get_running_loop()
    voice_manager.start_hotkey_listener(loop)

    async def on_voice_utterance(utterance: str, speech_end_time: float) -> None:
        await user_queue.put(("voice", utterance, speech_end_time))

    voice_manager.on_utterance = on_voice_utterance

    # Background reader thread for stdin
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
            if lower_input in (
                "restricted mode", "restricted", "go to restricted mode",
                "switch to restricted mode", "got to restricted mode"
            ):
                mode_manager.enter_restricted()
                print(f"[{persona_name}]: Switched to RESTRICTED mode.")
                await voice_manager.speak("Switched to Restricted mode.")
                continue
            elif lower_input in (
                "free mode", "free", "go to free mode",
                "switch to free mode", "got to free mode"
            ):
                pin_prompt = "Enter PIN to enable Free mode (or type/say 'cancel'): "
                print(f"\n[{persona_name}]: {pin_prompt}", end="", flush=True)
                await voice_manager.speak("Please enter the PIN to enable Free mode.")

                # Await user PIN response (typed in terminal or spoken via Push-to-Talk)
                pin_source, pin_input, pin_speech_end = await user_queue.get()
                if not pin_input or pin_input.strip().lower() in ("cancel", "no", "exit", "quit"):
                    cancel_msg = "Free mode cancelled. Remaining in Restricted mode."
                    print(f"\n[{persona_name}]: {cancel_msg}")
                    await voice_manager.speak(cancel_msg)
                    continue

                clean_pin = extract_pin_digits(pin_input)
                if mode_manager.enter_free(pin=clean_pin):
                    ok_msg = f"PIN verified. Switched to FREE mode (auto-revert in {free_timeout}s)."
                    print(f"\n[{persona_name}]: {ok_msg}")
                    await voice_manager.speak("PIN verified. Switched to Free mode.")
                else:
                    fail_msg = f"Incorrect PIN ('{pin_input}'). Remaining in Restricted mode."
                    print(f"\n[{persona_name}]: {fail_msg}")
                    await voice_manager.speak("Incorrect PIN. Remaining in Restricted mode.")
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
            _events.publish(
                "voice.processing",
                f"{persona_name} is thinking...",
                {"user_input": user_input[:60]},
            )
            reply, provider = await agent.run(messages)
            history.append({"role": "assistant", "content": reply})

            # ---- display reply ----
            tag: str = f"[{provider}]" if provider else "[unavailable]"
            print(f"\n{persona_name} {tag}:\n{reply}")

            # ---- voice narration ----
            await voice_manager.speak(reply, speech_end_time=speech_end_time)

    finally:
        ws_server.stop()
        voice_manager.stop_hotkey_listener()
        voice_manager.stop_speech()


def main() -> None:
    asyncio.run(chat_loop())


if __name__ == "__main__":
    main()
