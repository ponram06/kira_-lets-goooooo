"""
brain.py
========
Terminal chat loop with rolling conversation memory.

Run from the project root directory::

    python -m backend.brain

The system prompt and memory size come from config.yaml so the persona can
be switched without touching code.  After each reply, the name of the
provider that answered is printed in brackets.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import deque
from pathlib import Path
from typing import Optional

# Reconfigure stdout to UTF-8 on Windows so emoji characters never crash the terminal
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import yaml

from backend.agent import AgentRunner
from backend.policy import KILL_SWITCH, ModeManager
from backend.providers import ProviderManager

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
    deque and invokes AgentRunner with tools and policy enforcement.
    """
    cfg = _load_config(config_path)

    persona: dict = cfg.get("persona", {})
    system_prompt: str = persona.get("system_prompt", "You are a helpful assistant.")
    memory_limit: int = cfg.get("memory_limit", 20)
    persona_name: str = persona.get("name", "Kira").capitalize()
    free_timeout: int = cfg.get("mode", {}).get("free_revert_timeout", 600)

    manager = ProviderManager(config_path)
    mode_manager = ModeManager(free_timeout=free_timeout)
    agent = AgentRunner(provider_manager=manager, mode_manager=mode_manager)

    # Rolling memory: user + assistant turns combined
    history: deque[dict] = deque(maxlen=memory_limit)

    print("=" * 54)
    print(f"  {persona_name} — AI Dev Mentor")
    print(f"  Mode: {mode_manager.mode.value.upper()} | Type 'exit' or 'quit' to stop.")
    print("  Commands: 'restricted mode', 'free mode', 'stop', 'resume'")
    print("=" * 54)

    while True:
        # ---- read input ----
        try:
            mode_indicator = f"[{mode_manager.mode.value}]"
            user_input: str = input(f"\nYou {mode_indicator}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

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
            continue
        elif lower_input == "free mode":
            if mode_manager.enter_free():
                print(f"[{persona_name}]: Switched to FREE mode (auto-revert in {free_timeout}s).")
            else:
                print(f"[{persona_name}]: Free mode declined. Remaining in Restricted mode.")
            continue
        elif lower_input == "stop":
            KILL_SWITCH.stop()
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


def main() -> None:
    asyncio.run(chat_loop())


if __name__ == "__main__":
    main()

