"""
voice.py
========
Voice pipeline for Kira (Ears & Mouth).

Responsibilities
----------------
- Ears (STT):
    - Push-to-talk recording (hold hotkey, default F9, to record; release to send)
    - Groq hosted Whisper first (verbose_json), faster-whisper "small" as local backup
    - Independent STT cooldown tracker (separate from LLM chat provider cooldowns)
    - Confidence scoring (avg_logprob, no_speech_prob) and silence hallucination filtering
    - Discard recordings < 0.3s, cap recording at 30.0s
    - No audio saved to disk; in-memory processing only
- Mouth (TTS):
    - Edge TTS first (streaming MP3 decoded in-memory via miniaudio, no ffmpeg)
    - pyttsx3 local backup (fresh engine per call in dedicated thread with word callback interrupt)
    - Reply sentence-splitting with pipelined overlap: sentence N+1 synthesized while sentence N plays
    - Filters out code blocks ("The code is displayed on your screen"), URLs, and markdown symbols
    - Preserves decimals ("3.14") and abbreviations ("e.g.", "i.e.", "vs.", "etc.") without splitting
- Safety & Controls:
    - Voice CANNOT enter Free mode (instructs user to type it)
    - "restricted mode" spoken switches immediately to Restricted mode
    - Stop-word: only exact normalized "stop" or "kira stop" triggers the kill switch
    - Global emergency kill hotkey (default Ctrl+Alt+K) halts audio playback and sets KILL_SWITCH
    - Read-back confirmation only when policy check() returns 'ask'
    - Shared confirm function for typed and spoken answers (10s timer starts after read-back finishes)
- CLI:
    - python -m backend.voice --prepare: pre-downloads faster-whisper 'small' model
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Known silence / hallucination artifacts produced by Whisper models on near-silence
SILENCE_HALLUCINATIONS = frozenset({
    "thank you",
    "thank you.",
    "thank you very much.",
    "thanks for watching",
    "thanks for watching.",
    "thank you for watching.",
    "thank you for watching",
    "please subscribe",
    "please subscribe.",
    "subscribe",
    "subscribe.",
    "subtitles by",
    "subtitles by the amara.org community",
    "you",
    "bye",
    "bye.",
    "goodbye",
    "goodbye.",
})


def normalize_utterance(text: str) -> str:
    """
    Lowercase, strip punctuation and collapse whitespace for command matching.
    """
    if not text:
        return ""
    cleaned = re.sub(r"[^\w\s]", "", text.lower())
    return " ".join(cleaned.split()).strip()


def clean_spoken_text(text: str) -> str:
    """
    Sanitize LLM reply text for natural TTS narration:
    - Replace code blocks with 'The code is displayed on your screen.'
    - Replace inline code backticks with plain text
    - Replace raw URLs with 'the link'
    - Strip markdown headers, bold/italics, blockquotes, bullet points
    - Collapse redundant whitespace
    """
    if not text:
        return ""

    s = text

    # Replace fenced code blocks (```...```)
    s = re.sub(
        r"```[\s\S]*?```",
        " The code is displayed on your screen. ",
        s,
    )

    # Replace inline code backticks (`code`)
    s = re.sub(r"`([^`]+)`", r"\1", s)

    # Replace URLs (http:// or https://)
    s = re.sub(r"https?://\S+", "the link", s)

    # Strip markdown headers (e.g. # Header, ## Header)
    s = re.sub(r"^\s*#{1,6}\s+", "", s, flags=re.MULTILINE)

    # Strip bold and italics (*text*, **text**, _text_, __text__)
    s = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", s)

    # Strip markdown blockquotes (> Quote)
    s = re.sub(r"^\s*>\s*", "", s, flags=re.MULTILINE)

    # Strip list bullets (- item, * item)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.MULTILINE)

    # Collapse redundant whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def split_into_sentences(text: str) -> list[str]:
    """
    Split text into clean spoken sentences while preserving decimals
    like '3.14' and abbreviations like 'e.g.', 'i.e.', 'vs.', 'etc.'.
    """
    if not text or not text.strip():
        return []

    s = text.strip()

    # Protect decimals (digits followed by dot followed by digits)
    s = re.sub(r"(\d+)\.(\d+)", r"\1__DECIMAL_DOT__\2", s)

    # Protect common abbreviations that contain dots
    abbreviations = {
        "e.g.": "__ABBR_EG__",
        "i.e.": "__ABBR_IE__",
        "vs.": "__ABBR_VS__",
        "etc.": "__ABBR_ETC__",
        "mr.": "__ABBR_MR__",
        "mrs.": "__ABBR_MRS__",
        "ms.": "__ABBR_MS__",
        "dr.": "__ABBR_DR__",
        "prof.": "__ABBR_PROF__",
    }
    for abbr, token in abbreviations.items():
        s = re.sub(re.escape(abbr), token, s, flags=re.IGNORECASE)

    # Split on sentence terminators (. ! ?) followed by whitespace or end-of-string
    raw_parts = re.split(r"(?<=[.!?])\s+", s)

    sentences: list[str] = []
    rev_tokens = {v: k for k, v in abbreviations.items()}

    for part in raw_parts:
        # Restore decimal dots
        part = part.replace("__DECIMAL_DOT__", ".")
        # Restore abbreviation dots
        for token, orig in rev_tokens.items():
            part = part.replace(token, orig)
        part = part.strip()
        if part:
            sentences.append(part)

    return sentences
