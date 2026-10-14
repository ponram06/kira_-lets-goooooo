"""
voice_stub.py
=============
Kira Voice Output Tool (TTS).
Routes spoken messages through active VoiceManager (Edge TTS / pyttsx3)
and prints spoken narration to stdout.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ACTIVE_VOICE_MANAGER: Optional[Any] = None


def register_voice_manager(vm: Any) -> None:
    """Register the active VoiceManager instance for TTS output."""
    global _ACTIVE_VOICE_MANAGER
    _ACTIVE_VOICE_MANAGER = vm


def get_voice_manager() -> Optional[Any]:
    """Retrieve the registered VoiceManager instance."""
    return _ACTIVE_VOICE_MANAGER


def speak(text: str) -> str:
    """
    Voice output tool. Narrates text via TTS and prints to the console.

    Parameters
    ----------
    text:
        The text to be spoken.
    """
    print(f"\n[Kira speaking]: {text}\n")
    if _ACTIVE_VOICE_MANAGER is not None:
        try:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_ACTIVE_VOICE_MANAGER.speak(text))
            except RuntimeError:
                asyncio.run(_ACTIVE_VOICE_MANAGER.speak(text))
        except Exception as exc:
            logger.debug("TTS playback error in speak() tool: %s", exc)

    return f"Spoke: {text}"
