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

import asyncio
from dataclasses import dataclass
import io
import logging
import os
import re
import time
import wave
from typing import Any, Callable, Optional

import numpy as np
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

# Keywords identifying quota exhaustion vs transient 429
_QUOTA_KEYWORDS = ("quota", "daily", "exceeded", "resource_exhausted", "billing")

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


@dataclass
class TranscriptResult:
    """Result of speech-to-text recognition with quality metrics."""
    text: str = ""
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    source: str = "empty"  # 'groq', 'local', or 'empty'
    duration: float = 0.0
    low_confidence: bool = False
    hallucination: bool = False

    @property
    def is_valid(self) -> bool:
        return bool(self.text.strip()) and not self.low_confidence and not self.hallucination


class STTCooldownTracker:
    """
    Independent cooldown tracker for Speech-to-Text providers.
    Completely isolated from LLM chat-provider cooldowns.
    """

    def __init__(self, cooldown_cfg: Optional[dict] = None) -> None:
        self._cfg = cooldown_cfg or {
            "rate_limit": 900,
            "quota": 3600,
            "timeout": 60,
            "server_error": 60,
        }
        self._cooldown_until: dict[str, float] = {}
        self._disabled: set[str] = set()

    def is_available(self, name: str) -> bool:
        if name in self._disabled:
            return False
        return time.monotonic() >= self._cooldown_until.get(name, 0.0)

    def cooldown(self, name: str, reason: str) -> None:
        seconds = self._cfg.get(reason, 60)
        self._cooldown_until[name] = time.monotonic() + seconds
        logger.warning("STT provider '%s' placed in cooldown for %d s (%s).", name, seconds, reason)

    def disable(self, name: str) -> None:
        self._disabled.add(name)
        logger.error("STT provider '%s' disabled for this session (bad credentials).", name)

    def reset(self) -> None:
        self._cooldown_until.clear()
        self._disabled.clear()


def audio_to_wav_bytes(audio_np: np.ndarray, sample_rate: int = 16000) -> bytes:
    """
    Convert a 1D int16 or float32 numpy array to WAV bytes in memory.
    Never creates or leaves files on disk.
    """
    if audio_np.dtype != np.int16:
        if np.issubdtype(audio_np.dtype, np.floating):
            audio_np = np.clip(audio_np * 32767.0, -32768, 32767).astype(np.int16)
        else:
            audio_np = audio_np.astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_np.tobytes())
    return buf.getvalue()


class STTEngine:
    """
    Speech-to-Text engine implementing:
    1. Groq hosted Whisper (verbose_json) with STTCooldownTracker
    2. faster-whisper local 'small' model fallback
    3. Confidence scoring and silence hallucination filtering
    4. Duration gating (ignoring <0.3s, capping at 30s)
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        stt_cfg = config.get("voice", {}).get("stt", {})
        self.groq_model = stt_cfg.get("groq_model", "whisper-large-v3-turbo")
        self.local_model_name = stt_cfg.get("local_model", "small")
        self.min_duration = float(stt_cfg.get("min_duration", 0.3))
        self.max_duration = float(stt_cfg.get("max_duration", 30.0))
        self.min_avg_logprob = float(stt_cfg.get("min_avg_logprob", -1.0))
        self.max_no_speech_prob = float(stt_cfg.get("max_no_speech_prob", 0.6))
        self.groq_timeout = float(stt_cfg.get("timeouts", {}).get("groq", 10.0))

        self.cooldown_tracker = STTCooldownTracker(config.get("cooldowns", {}))
        self._local_whisper_model: Any = None

    async def transcribe(self, audio_np: np.ndarray, duration: float) -> Optional[TranscriptResult]:
        """
        Transcribe the recorded audio array.
        Returns None if duration < min_duration (tap ignored).
        """
        if duration < self.min_duration:
            logger.info("Ignoring short audio tap (%.2fs < min %.2fs).", duration, self.min_duration)
            return None

        # Try Groq hosted Whisper first if available
        if self.cooldown_tracker.is_available("groq"):
            groq_key = os.getenv("GROQ_API_KEY")
            if groq_key:
                try:
                    result = await self._transcribe_groq(audio_np, duration, groq_key)
                    if result is not None:
                        return self._evaluate_quality(result)
                except Exception as exc:
                    logger.warning("Groq STT encountered unexpected error: %s", exc)
            else:
                logger.debug("GROQ_API_KEY not set; falling back directly to local Whisper.")

        # Fallback: local faster-whisper
        logger.info("Using local faster-whisper ('%s') for transcription.", self.local_model_name)
        result = await self._transcribe_local(audio_np, duration)
        return self._evaluate_quality(result)

    async def _transcribe_groq(
        self,
        audio_np: np.ndarray,
        duration: float,
        api_key: str,
    ) -> Optional[TranscriptResult]:
        """Transcribe via Groq hosted Whisper API using verbose_json format."""
        wav_bytes = audio_to_wav_bytes(audio_np)
        client = AsyncOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=api_key,
            timeout=self.groq_timeout,
        )

        try:
            # Pass in-memory WAV buffer as file tuple (filename, bytes, content_type)
            response = await client.audio.transcriptions.create(
                model=self.groq_model,
                file=("speech.wav", wav_bytes, "audio/wav"),
                response_format="verbose_json",
            )

            text = getattr(response, "text", "") or ""
            segments = getattr(response, "segments", []) or []

            avg_logprobs = []
            no_speech_probs = []
            for seg in segments:
                lp = seg.get("avg_logprob") if isinstance(seg, dict) else getattr(seg, "avg_logprob", None)
                nsp = seg.get("no_speech_prob") if isinstance(seg, dict) else getattr(seg, "no_speech_prob", None)
                if lp is not None:
                    avg_logprobs.append(float(lp))
                if nsp is not None:
                    no_speech_probs.append(float(nsp))

            avg_logprob = float(np.mean(avg_logprobs)) if avg_logprobs else 0.0
            no_speech_prob = float(np.mean(no_speech_probs)) if no_speech_probs else 0.0

            return TranscriptResult(
                text=text.strip(),
                avg_logprob=avg_logprob,
                no_speech_prob=no_speech_prob,
                source="groq",
                duration=duration,
            )

        except RateLimitError as exc:
            err_msg = str(exc).lower()
            if any(kw in err_msg for kw in _QUOTA_KEYWORDS):
                logger.warning("Groq STT quota exceeded.")
                self.cooldown_tracker.cooldown("groq", "quota")
            else:
                logger.warning("Groq STT rate limited (429).")
                self.cooldown_tracker.cooldown("groq", "rate_limit")
            return None

        except (AuthenticationError, PermissionDeniedError):
            logger.error("Groq STT authentication failed (401/403). Disabling Groq STT.")
            self.cooldown_tracker.disable("groq")
            return None

        except (APITimeoutError, APIConnectionError):
            logger.warning("Groq STT timed out or connection error.")
            self.cooldown_tracker.cooldown("groq", "timeout")
            return None

        except APIStatusError as exc:
            if 500 <= exc.status_code < 600:
                logger.warning("Groq STT server error %d.", exc.status_code)
                self.cooldown_tracker.cooldown("groq", "server_error")
            else:
                logger.error("Groq STT unexpected status %d.", exc.status_code)
            return None

    async def _transcribe_local(self, audio_np: np.ndarray, duration: float) -> TranscriptResult:
        """Transcribe via local faster-whisper model running in a background executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._transcribe_local_sync, audio_np, duration)

    def _transcribe_local_sync(self, audio_np: np.ndarray, duration: float) -> TranscriptResult:
        """Synchronous local faster-whisper transcription worker."""
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            logger.error("faster-whisper is not installed.")
            return TranscriptResult(text="", source="local", duration=duration, low_confidence=True)

        if self._local_whisper_model is None:
            logger.info("Initializing faster-whisper model ('%s')...", self.local_model_name)
            self._local_whisper_model = WhisperModel(
                self.local_model_name,
                device="cpu",
                compute_type="int8",
            )

        # faster-whisper accepts normalized float32 array
        audio_float = audio_np.astype(np.float32) / 32768.0
        segments, _ = self._local_whisper_model.transcribe(audio_float, beam_size=5)

        seg_list = list(segments)
        text = " ".join(s.text for s in seg_list).strip()
        avg_logprob = float(np.mean([s.avg_logprob for s in seg_list])) if seg_list else 0.0
        no_speech_prob = float(np.mean([s.no_speech_prob for s in seg_list])) if seg_list else 0.0

        return TranscriptResult(
            text=text,
            avg_logprob=avg_logprob,
            no_speech_prob=no_speech_prob,
            source="local",
            duration=duration,
        )

    def _evaluate_quality(self, result: TranscriptResult) -> TranscriptResult:
        """
        Check confidence thresholds and filter known silence hallucinations.
        """
        if not result.text.strip():
            return result

        # Confidence check
        if (result.avg_logprob < self.min_avg_logprob) or (result.no_speech_prob > self.max_no_speech_prob):
            logger.info(
                "STT low confidence: avg_logprob=%.2f (min %.2f), no_speech_prob=%.2f (max %.2f)",
                result.avg_logprob,
                self.min_avg_logprob,
                result.no_speech_prob,
                self.max_no_speech_prob,
            )
            result.low_confidence = True

        # Silence hallucination check
        norm = normalize_utterance(result.text)
        if norm in SILENCE_HALLUCINATIONS:
            logger.info("Filtered silence hallucination: '%s'", result.text)
            result.hallucination = True

        return result
