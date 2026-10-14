"""
voice_stub.py
=============
Temporary stub for TTS output. Prints spoken messages to stdout.
"""

from __future__ import annotations


def speak(text: str) -> str:
    """
    Voice output stub. Prints narration to the console.

    Parameters
    ----------
    text:
        The text to be spoken.
    """
    print(f"\n[Kira speaking]: {text}\n")
    return f"Spoke: {text}"
