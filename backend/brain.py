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
from collections import deque
from pathlib import Path
from typing import Optional

import yaml

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
    Main async REPL.  Keeps the last *memory_limit* messages in a rolling
    deque so the LLM has recent context without unbounded token growth.
    """
    cfg = _load_config(config_path)

    persona: dict = cfg.get("persona", {})
    system_prompt: str = persona.get("system_prompt", "You are a helpful assistant.")
    memory_limit: int = cfg.get("memory_limit", 20)
    persona_name: str = persona.get("name", "Kira").capitalize()

    manager = ProviderManager(config_path)

    # Rolling memory: user + assistant turns combined
    history: deque[dict] = deque(maxlen=memory_limit)

    print("=" * 54)
    print(f"  {persona_name} — AI Dev Mentor")
    print("  Type 'exit' or 'quit' to stop.")
    print("=" * 54)

    while True:
        # ---- read input ----
        try:
            user_input: str = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Goodbye.")
            break

        # ---- build message list ----
        history.append({"role": "user", "content": user_input})
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            *history,
        ]

        # ---- call LLM ----
        reply, provider = await manager.ask(messages)
        history.append({"role": "assistant", "content": reply})

        # ---- display reply ----
        tag: str = f"[{provider}]" if provider else "[unavailable]"
        print(f"\n{persona_name} {tag}:\n{reply}")


def main() -> None:
    asyncio.run(chat_loop())


if __name__ == "__main__":
    main()

