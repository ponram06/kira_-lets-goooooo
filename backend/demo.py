"""
demo.py
=======
One-click manual test runner for the canonical AGENTS.md demo scenario:
"Open VS Code, create a folder called demo, make hello.py with hello world, and run it."

Usage
-----
    python -m backend.demo

Expected Result:
1. VS Code launches on workspace/demo with hello.py open.
2. The code `print('Hello, World!')` is typed visibly into the editor and saved.
3. A visible Windows CMD window opens and runs `python hello.py`.
4. Output is displayed in the CMD window and captured back to Kira.
5. Kira narrates out loud: "Here is the output in the terminal: Hello, World!"
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import sys

# Ensure UTF-8 output on Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import yaml

from backend.agent import AgentRunner
from backend.policy import KILL_SWITCH, ModeManager
from backend.providers import ProviderManager
from backend.tools.voice_stub import register_voice_manager
from backend.voice import VoiceManager

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

DEMO_PROMPT = (
    "Open VS Code, create a folder called demo, make hello.py with hello world, and run it."
)

DEMO_SYSTEM_PROMPT = """You are Kira, a calm and strategic AI mentor for software developers.
When performing tasks on the computer you narrate each action concisely before taking it.

For creating and opening code files in VS Code, use the open_in_vscode tool.
For running Python scripts inside workspace, use the run_command tool.

IMPORTANT: When you finish running the script, your final response must end with:
"Here is the output in the terminal: <output>"
where <output> is the exact text produced by the script (e.g. Hello, World!).
"""

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s  %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run_demo(config_path: Path = _CONFIG_PATH) -> None:
    print("=" * 60)
    print("  Kira Desktop Assistant — Canonical Demo Flow")
    print(f"  Task: \"{DEMO_PROMPT}\"")
    print("=" * 60 + "\n")

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    provider_mgr = ProviderManager(config_path)
    mode_mgr = ModeManager()
    voice_mgr = VoiceManager(cfg, mode_manager=mode_mgr)
    register_voice_manager(voice_mgr)

    # In Restricted mode, allow-listed tools in workspace/ run without prompting
    agent = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=mode_mgr,
        confirm_callback=lambda prompt: True,
    )

    messages = [
        {"role": "system", "content": DEMO_SYSTEM_PROMPT},
        {"role": "user", "content": DEMO_PROMPT},
    ]

    print("[Kira]: Beginning demo execution...\n")
    reply, provider = await agent.run(messages)
    tag = f"[{provider}]" if provider else "[agent]"
    print(f"\nKira {tag}:\n{reply}\n")

    # Spoken narration of the final report
    print("[Kira]: Speaking narration out loud via TTS...")
    await voice_mgr.speak(reply)

    print("\n" + "=" * 60)
    print("  Demo complete!")
    print("=" * 60 + "\n")


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
