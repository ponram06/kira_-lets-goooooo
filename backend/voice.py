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
import queue
import re
import sys
import threading
import time
import uuid
import wave
from typing import Any, Callable, Optional

import edge_tts
import miniaudio
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
from pynput import keyboard
import sounddevice as sd

from backend.policy import KILL_SWITCH
from backend import events as _events

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

        # Compute audio energy (RMS) to detect silence / ambient typing clicks
        audio_float = audio_np.astype(np.float32)
        if audio_np.dtype == np.int16:
            audio_rms = float(np.sqrt(np.mean(audio_float ** 2)))
        else:
            audio_rms = float(np.sqrt(np.mean((audio_float * 32768.0) ** 2)))

        # Try Groq hosted Whisper first if available
        if self.cooldown_tracker.is_available("groq"):
            groq_key = os.getenv("GROQ_API_KEY")
            if groq_key:
                try:
                    result = await self._transcribe_groq(audio_np, duration, groq_key)
                    if result is not None:
                        return self._evaluate_quality(result, audio_rms=audio_rms)
                except Exception as exc:
                    logger.warning("Groq STT encountered unexpected error: %s", exc)
            else:
                logger.debug("GROQ_API_KEY not set; falling back directly to local Whisper.")

        # Fallback: local faster-whisper
        logger.info("Using local faster-whisper ('%s') for transcription.", self.local_model_name)
        result = await self._transcribe_local(audio_np, duration)
        return self._evaluate_quality(result, audio_rms=audio_rms)

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

    def _evaluate_quality(self, result: TranscriptResult, audio_rms: float = 0.0) -> TranscriptResult:
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
        if norm in SILENCE_HALLUCINATIONS or (
            audio_rms > 0.0
            and audio_rms < 250.0
            and norm in {"yes", "yeah", "yup", "no", "uh", "um", "ah", "ok", "okay"}
        ):
            logger.info("Filtered silence hallucination (rms=%.1f): '%s'", audio_rms, result.text)
            result.hallucination = True

        return result


class TTSEngine:
    """
    Text-to-Speech engine implementing:
    1. Edge TTS with in-memory MP3 decoding via miniaudio (pure C, no ffmpeg)
    2. pyttsx3 fallback running in a dedicated thread with a fresh engine per call
    3. Sentence-level synthesis/playback pipelining (synthesizes sentence N+1 while playing sentence N)
    4. Instantaneous interruption on kill-switch, 'stop' word, or hotkey press
    5. Clean spoken text filtering (replaces code blocks, URLs, markdown syntax)
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        tts_cfg = config.get("voice", {}).get("tts", {})
        self.edge_voice = tts_cfg.get("edge_voice", "en-US-GuyNeural")
        self.edge_rate = tts_cfg.get("edge_rate", "+0%")
        self.backup_engine = tts_cfg.get("backup_engine", "pyttsx3")

        self._interrupted = threading.Event()
        self._current_pyttsx3_stop = threading.Event()

    def stop(self) -> None:
        """Immediately halt all audio synthesis and playback."""
        self._interrupted.set()
        self._current_pyttsx3_stop.set()
        try:
            sd.stop()
        except Exception as exc:
            logger.debug("sounddevice.stop error: %s", exc)

    def is_interrupted(self) -> bool:
        return self._interrupted.is_set() or KILL_SWITCH.is_stopped()

    async def synthesize_sentence_edge(self, sentence: str) -> Optional[tuple[np.ndarray, int]]:
        """
        Synthesize a single sentence via Edge TTS and decode MP3 directly to raw PCM
        using miniaudio in memory. Returns (pcm_samples, sample_rate) or None on failure.
        """
        if self.is_interrupted() or not sentence.strip():
            return None

        try:
            communicate = edge_tts.Communicate(sentence, voice=self.edge_voice, rate=self.edge_rate)
            chunks: list[bytes] = []
            async for chunk in communicate.stream():
                if self.is_interrupted():
                    return None
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])

            if not chunks:
                return None

            mp3_bytes = b"".join(chunks)
            decoded = miniaudio.mp3_read_s16(mp3_bytes)
            samples = np.frombuffer(decoded.samples, dtype=np.int16)
            return (samples, decoded.sample_rate)

        except Exception as exc:
            logger.warning("Edge TTS synthesis failed for '%s': %s", sentence[:30], exc)
            return None

    def _speak_pyttsx3_sync(self, sentence: str, stop_event: threading.Event) -> None:
        """
        Run pyttsx3 with a fresh engine per call inside a worker thread.
        Uses on_word callback for sub-second interrupt response.
        """
        if self.is_interrupted() or not sentence.strip():
            return

        try:
            import pyttsx3
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass

        try:
            import pyttsx3
            engine = pyttsx3.init()

            def on_word(name: Any, location: Any, length: Any) -> None:
                if stop_event.is_set() or self.is_interrupted():
                    engine.stop()
                else:
                    _events.publish("voice.loudness", "speech loudness", {"level": 0.6, "value": 0.6})

            try:
                engine.connect("word", on_word)
            except Exception:
                pass

            engine.say(sentence)
            engine.runAndWait()
        except Exception as exc:
            logger.warning("pyttsx3 speech failed: %s", exc)
        finally:
            _events.publish("voice.loudness", "speech loudness", {"level": 0.0, "value": 0.0})
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass

    async def play_pcm(self, samples: np.ndarray, sample_rate: int) -> None:
        """Play PCM audio through sounddevice with quick interrupt polling and loudness streaming."""
        if self.is_interrupted() or len(samples) == 0:
            return

        try:
            chunk_size = max(1, int(sample_rate * 0.05))  # ~50ms chunks (20Hz)
            total_samples = len(samples)
            start_time = time.monotonic()

            sd.play(samples, samplerate=sample_rate)
            while sd.get_stream() and sd.get_stream().active:
                if self.is_interrupted():
                    sd.stop()
                    break

                elapsed = time.monotonic() - start_time
                current_idx = int(elapsed * sample_rate)
                if current_idx < total_samples:
                    chunk = samples[current_idx : min(total_samples, current_idx + chunk_size)]
                    if len(chunk) > 0:
                        rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
                        norm_loudness = min(1.0, max(0.0, rms / 8000.0))
                        _events.publish(
                            "voice.loudness",
                            "speech loudness",
                            {"level": round(norm_loudness, 3), "value": round(norm_loudness, 3)},
                        )

                await asyncio.sleep(0.04)
        except sd.PortAudioError as exc:
            logger.warning("Sound output device unavailable (PortAudioError): %s", exc)
        except Exception as exc:
            logger.warning("Playback error: %s", exc)
        finally:
            _events.publish("voice.loudness", "speech loudness", {"level": 0.0, "value": 0.0})

    async def speak(self, text: str, speech_end_time: Optional[float] = None) -> None:
        """
        Speak the provided text using sentence-pipelined synthesis:
        Sentence N+1 is synthesized in the background while sentence N is playing.
        If Edge TTS fails, falls back to pyttsx3.
        """
        self._interrupted.clear()
        self._current_pyttsx3_stop.clear()

        cleaned = clean_spoken_text(text)
        if not cleaned:
            return

        sentences = split_into_sentences(cleaned)
        if not sentences:
            return

        first_word_spoken = False
        loop = asyncio.get_running_loop()

        _events.publish("voice.speaking", f"Speaking: {cleaned[:60]}", {"text": cleaned[:120]})

        try:
            # Try Edge TTS pipeline first
            edge_failed = False
            synth_task: Optional[asyncio.Task] = asyncio.create_task(self.synthesize_sentence_edge(sentences[0]))

            for i, sentence in enumerate(sentences):
                if self.is_interrupted():
                    if synth_task and not synth_task.done():
                        synth_task.cancel()
                    break

                current_audio = None
                if not edge_failed and synth_task:
                    try:
                        current_audio = await synth_task
                    except Exception:
                        current_audio = None

                # If Edge failed on this sentence, fallback to pyttsx3
                if current_audio is None and not self.is_interrupted():
                    edge_failed = True

                if edge_failed:
                    # Use pyttsx3 fallback
                    if not first_word_spoken and speech_end_time is not None:
                        latency = time.monotonic() - speech_end_time
                        print(f"\n[Voice latency]: End of speech to first spoken word: {latency:.2f}s (target <3s)")
                        first_word_spoken = True

                    await loop.run_in_executor(
                        None,
                        self._speak_pyttsx3_sync,
                        sentence,
                        self._current_pyttsx3_stop,
                    )
                else:
                    # Edge TTS succeeded: kick off synthesis for the next sentence concurrently
                    next_task: Optional[asyncio.Task] = None
                    if i + 1 < len(sentences) and not self.is_interrupted():
                        next_task = asyncio.create_task(self.synthesize_sentence_edge(sentences[i + 1]))

                    if not first_word_spoken and speech_end_time is not None:
                        latency = time.monotonic() - speech_end_time
                        print(f"\n[Voice latency]: End of speech to first spoken word: {latency:.2f}s (target <3s)")
                        first_word_spoken = True

                    if current_audio:
                        pcm_data, sr = current_audio
                        await self.play_pcm(pcm_data, sr)

                    synth_task = next_task
        finally:
            _events.publish("voice.done", "Voice playback finished", {"interrupted": self.is_interrupted()})


def check_local_model_available(model_name: str = "small") -> bool:
    """Check if the faster-whisper model is already cached locally without accessing internet."""
    try:
        from faster_whisper import download_model
        download_model(model_name, local_files_only=True)
        return True
    except Exception:
        return False


def prepare_local_model(model_name: str = "small") -> bool:
    """Download and cache the faster-whisper model for offline backup."""
    print(f"\n[Kira]: Pre-downloading faster-whisper '{model_name}' model for offline backup...")
    try:
        from faster_whisper import download_model
        path = download_model(model_name, local_files_only=False)
        print(f"[Kira]: Successfully prepared faster-whisper '{model_name}' model at: {path}\n")
        return True
    except Exception as exc:
        print(f"[Kira]: Failed to download faster-whisper '{model_name}' model: {exc}\n")
        return False


def parse_hotkey_spec(spec: str) -> Any:
    """Parse a single key name (e.g. 'F9', '`', 'backtick') into a pynput keyboard.Key or KeyCode."""
    s = spec.strip().lower()
    aliases = {
        "backtick": "`",
        "grave": "`",
        "backquote": "`",
        "`": "`",
        "tilde": "~",
        "~": "~",
    }
    if s in aliases:
        return keyboard.KeyCode.from_char(aliases[s])
    if hasattr(keyboard.Key, s):
        return getattr(keyboard.Key, s)
    if s.startswith("f") and s[1:].isdigit():
        return getattr(keyboard.Key, s, None)
    return keyboard.KeyCode.from_char(s)


class VoiceManager:
    """
    Coordinates Voice Ears (STT), Mouth (TTS), Hotkeys, and Safety Gates.
    """

    def __init__(
        self,
        config: dict,
        mode_manager: Any = None,
        on_utterance: Optional[Callable[[str, float], Any]] = None,
    ) -> None:
        self.config = config
        self.mode_manager = mode_manager
        self.on_utterance = on_utterance

        voice_cfg = config.get("voice", {})
        self.ptt_hotkey_name = voice_cfg.get("hotkey", "F9")
        self.kill_hotkey_name = voice_cfg.get("kill_hotkey", "ctrl+alt+k")

        self.talk_key = parse_hotkey_spec(self.ptt_hotkey_name)
        self.min_duration = float(voice_cfg.get("stt", {}).get("min_duration", 0.3))
        self.max_duration = float(voice_cfg.get("stt", {}).get("max_duration", 30.0))

        # Mute state
        self.is_muted_mic = False
        self.is_muted_voice = False

        # Privacy notice
        self.privacy_notice = voice_cfg.get(
            "safety", {}
        ).get("privacy_notice", "Audio may be sent to Groq for speech recognition.")

        # Sub-engines
        self.stt = STTEngine(config)
        self.tts = TTSEngine(config)

        # Hotkey tracking (only talk key and kill key tracked, never other keys)
        self._ctrl_down = False
        self._alt_down = False
        self._talk_key_held = False
        self._keyboard_listener: Optional[keyboard.Listener] = None

        # Recording state
        self._is_recording = False
        self._recorded_frames: list[np.ndarray] = []
        self._record_start_time = 0.0
        self._input_stream: Optional[sd.InputStream] = None

        # Event loop reference for bridging worker thread callbacks
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Missing model warning at startup
        local_model = self.stt.local_model_name
        if not check_local_model_available(local_model):
            print(
                f"\n[Kira voice warning]: Local backup Whisper model '{local_model}' is not downloaded. "
                f"Run 'python -m backend.voice --prepare' to enable offline STT fallback.\n"
            )

    def print_privacy_notice(self) -> None:
        """Display transparent privacy notice regarding cloud speech recognition."""
        print(f"\n[Kira Voice]: {self.privacy_notice}\n")

    def mute_mic(self, muted: bool = True) -> None:
        """Toggle microphone recording on/off."""
        self.is_muted_mic = muted
        status = "MUTED" if muted else "UNMUTED"
        print(f"[Kira Voice]: Microphone is now {status}.")

    def mute_voice(self, muted: bool = True) -> None:
        """Toggle Kira speech playback on/off."""
        self.is_muted_voice = muted
        status = "MUTED" if muted else "UNMUTED"
        print(f"[Kira Voice]: Assistant voice is now {status}.")

    async def speak(self, text: str, speech_end_time: Optional[float] = None) -> None:
        """Speak narration out loud unless voice output is muted."""
        if self.is_muted_voice:
            return
        await self.tts.speak(text, speech_end_time=speech_end_time)

    def stop_speech(self) -> None:
        """Halt any ongoing speech synthesis or playback immediately."""
        self.tts.stop()

    def start_hotkey_listener(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Start global hotkey listener for Push-to-Talk and emergency kill."""
        self._loop = loop or asyncio.get_event_loop()
        if self._keyboard_listener is None:
            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            )
            self._keyboard_listener.daemon = True
            self._keyboard_listener.start()
            logger.info("Voice hotkey listener started (PTT: %s, Kill: %s)", self.ptt_hotkey_name, self.kill_hotkey_name)

    def stop_hotkey_listener(self) -> None:
        """Stop global hotkey listener."""
        if self._keyboard_listener:
            try:
                self._keyboard_listener.stop()
            except Exception:
                pass
            self._keyboard_listener = None

    def _is_talk_key(self, key: Any) -> bool:
        """
        Check if a key event matches the configured Push-to-Talk hotkey.
        Handles direct equality, char matching (case-insensitive & dead-key immune),
        and Windows virtual key codes (VK 192 for ` / ~).
        """
        if key == self.talk_key:
            return True
        t_char = getattr(self.talk_key, "char", None)
        k_char = getattr(key, "char", None)
        if t_char and k_char and t_char.lower() == k_char.lower():
            return True
        if t_char in ("`", "~") and getattr(key, "vk", None) == 192:
            return True
        return False

    def _on_press(self, key: Any) -> None:
        """
        Hotkey press handler.
        Acts ONLY on talk key and kill key. Ignores key repeat and never logs other keys.
        """
        # Modifier tracking
        if key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl_down = True
        elif key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r):
            self._alt_down = True

        # Emergency kill hotkey check (Ctrl+Alt+K)
        k_char = getattr(key, "char", None)
        if self._ctrl_down and self._alt_down and (k_char in ("k", "K") or key == keyboard.KeyCode.from_char("k")):
            KILL_SWITCH.stop()
            self.stop_speech()
            print("\n[EMERGENCY KILL SWITCH: Ctrl+Alt+K pressed. All actions and audio halted.]\n")
            return

        # Talk hotkey check (e.g. F9, `)
        if self._is_talk_key(key):
            # Debounce key-repeat
            if self._talk_key_held:
                return
            self._talk_key_held = True

            # Interrupt ongoing Kira speech immediately
            self.stop_speech()

            if self.is_muted_mic:
                print("\n[Kira Voice]: Microphone is muted. Press unmute to speak.\n")
                return

            self._start_recording()
            _events.publish(
                "voice.listening",
                f"Listening (hold {self.ptt_hotkey_name})...",
                {"hotkey": self.ptt_hotkey_name},
            )
            print(f"\n[Kira listening... (hold {self.ptt_hotkey_name}, release to send)]")

    def _on_release(self, key: Any) -> None:
        """Hotkey release handler."""
        if key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl_down = False
        elif key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r):
            self._alt_down = False

        if self._is_talk_key(key):
            self._talk_key_held = False
            if self._is_recording:
                audio_np, duration, speech_end_time = self._stop_recording()
                if audio_np is not None and duration >= self.min_duration:
                    print(f"\n[Processing speech ({duration:.1f}s)...]", flush=True)
                    _events.publish(
                        "voice.processing",
                        f"Processing audio ({duration:.1f}s)...",
                        {"duration": round(duration, 2)},
                    )
                    if self._loop and self._loop.is_running():
                        fut = asyncio.run_coroutine_threadsafe(
                            self.handle_recorded_audio(audio_np, duration, speech_end_time),
                            self._loop,
                        )

                        def _on_future_done(f: Any) -> None:
                            try:
                                f.result()
                            except Exception as exc:
                                print(f"\n[Kira Voice Error]: {exc}\n", flush=True)

                        fut.add_done_callback(_on_future_done)
                else:
                    tap_dur = duration if audio_np is not None else 0.0
                    print(
                        f"\n[Audio tap too short ({tap_dur:.2f}s < min {self.min_duration:.1f}s) — hold {self.ptt_hotkey_name} while speaking]\n",
                        flush=True,
                    )

    def _start_recording(self) -> None:
        """Begin capturing microphone frames into memory."""
        self._recorded_frames = []
        self._record_start_time = time.monotonic()
        self._is_recording = True

        def _callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            if self._is_recording:
                self._recorded_frames.append(indata.copy())
                if (time.monotonic() - self._record_start_time) >= self.max_duration:
                    self._is_recording = False

        try:
            self._input_stream = sd.InputStream(
                samplerate=16000,
                channels=1,
                dtype="int16",
                callback=_callback,
            )
            self._input_stream.start()
        except Exception as exc:
            logger.warning("Could not open microphone input stream: %s", exc)
            self._is_recording = False
            self._input_stream = None

    def _stop_recording(self) -> tuple[Optional[np.ndarray], float, float]:
        """Stop capturing and return concatenated audio array in memory."""
        self._is_recording = False
        speech_end_time = time.monotonic()
        duration = speech_end_time - self._record_start_time

        if self._input_stream:
            try:
                self._input_stream.stop()
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None

        if not self._recorded_frames:
            return None, duration, speech_end_time

        audio_np = np.concatenate(self._recorded_frames, axis=0).flatten()
        return audio_np, duration, speech_end_time

    async def handle_recorded_audio(
        self,
        audio_np: np.ndarray,
        duration: float,
        speech_end_time: float,
    ) -> Optional[str]:
        """
        Process recorded audio:
        1. Transcribe via Groq -> faster-whisper
        2. Evaluate quality and hallucinations
        3. Print recognized text
        4. Intercept stop word and mode commands locally BEFORE LLM
        5. Forward valid task utterance to agent callback
        """
        try:
            result = await self.stt.transcribe(audio_np, duration)
            if result is None:
                return None

            # Print recognized text before acting on it
            if result.text.strip():
                print(f"\nYou [voice]: {result.text}", flush=True)

            # Quality & hallucination handling
            if not result.is_valid:
                if result.hallucination:
                    print(f"[Notice: Silence or background sound ('{result.text}'). Please speak clearly.]", flush=True)
                    return None
                if result.low_confidence or not result.text.strip():
                    clarify_msg = "I didn't quite catch that. Could you please repeat?"
                    print(f"[Kira]: {clarify_msg}", flush=True)
                    await self.speak(clarify_msg)
                    return None

            utterance = result.text.strip()
            norm = normalize_utterance(utterance)

            # 1. Stop-word: only exact normalized "stop" or "kira stop"
            if norm in ("stop", "kira stop"):
                KILL_SWITCH.stop()
                self.stop_speech()
                stop_msg = "Kill switch ACTIVATED by voice command ('stop'). All actions halted."
                print(f"[Kira]: {stop_msg}", flush=True)
                return None

            # 2. Free mode command handling
            if norm in ("free mode", "go to free mode", "switch to free mode", "got to free mode"):
                if self.on_utterance:
                    await self.on_utterance(utterance, speech_end_time)
                    return utterance
                else:
                    free_msg = "Free mode cannot be enabled by voice. Please type 'free mode' in the terminal."
                    print(f"[Kira]: {free_msg}", flush=True)
                    await self.speak(free_msg)
                    return None

            # 3. Restricted mode allowed by voice
            if norm in ("restricted mode", "go to restricted mode", "switch to restricted mode", "got to restricted mode"):
                if self.mode_manager:
                    self.mode_manager.enter_restricted()
                restr_msg = "Switched to Restricted mode."
                print(f"[Kira]: {restr_msg}", flush=True)
                await self.speak(restr_msg)
                return None

            # Forward valid utterance to callback if configured
            if self.on_utterance:
                await self.on_utterance(utterance, speech_end_time)

            return utterance
        except Exception as exc:
            logger.exception("Error in handle_recorded_audio: %s", exc)
            print(f"\n[Kira Voice Error]: {exc}\n", flush=True)
            return None


# Thread-safe external confirmation state (WebSocket, etc.)
_active_confirm_lock = threading.Lock()
_active_confirm_question_id: Optional[str] = None
_active_confirm_queue: Optional[Any] = None


def register_confirmation_response(question_id: str, source: str, answer: str) -> bool:
    """
    Submit a confirmation response from an external channel (e.g. WebSocket).

    Returns True if accepted (question_id matches the active pending prompt),
    False if stale, mismatched, or no question is active.
    """
    with _active_confirm_lock:
        if _active_confirm_queue is None or _active_confirm_question_id is None:
            logger.debug("Confirmation received with no active question: %s", answer)
            return False
        if _active_confirm_question_id != question_id:
            logger.warning(
                "Stale or mismatched confirmation question_id: got '%s', expected '%s'",
                question_id,
                _active_confirm_question_id,
            )
            return False
        _active_confirm_queue.put((source, answer))
        return True


def get_active_question_id() -> Optional[str]:
    """Return the currently pending confirmation question_id, or None if idle."""
    with _active_confirm_lock:
        return _active_confirm_question_id


def shared_confirm(
    prompt_text: str,
    voice_manager: Optional[VoiceManager] = None,
    timeout_seconds: float = 10.0,
) -> bool:
    """
    Shared confirmation gate supporting typed, spoken, and WebSocket responses.
    First response wins.

    Rules
    -----
    - Kira reads back the action out loud first.
    - The 10-second timer begins AFTER Kira finishes speaking.
    - Generates a unique random question_id and publishes a policy.ask event.
    - External (WebSocket) answers must supply matching question_id.
    - Only an explicit fresh 'yes' or 'y' counts as approval.
    - Anything else or no answer (timeout) means 'no' (declined).
    """
    global _active_confirm_question_id, _active_confirm_queue
    question_id = uuid.uuid4().hex[:8]

    # 1. Read-back narration (TTS) before starting timer
    read_back = f"Confirmation required. {prompt_text}. Do you approve? Say or type yes to proceed."
    if voice_manager and not voice_manager.is_muted_voice:
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(voice_manager.speak(read_back), loop)
                try:
                    fut.result(timeout=5.0)
                except Exception as exc:
                    logger.debug("Read-back TTS error: %s", exc)
            else:
                asyncio.run(voice_manager.speak(read_back))
        except Exception as exc:
            logger.debug("Read-back TTS error: %s", exc)

    result_queue: queue.Queue[tuple[str, str]] = queue.Queue()

    with _active_confirm_lock:
        _active_confirm_question_id = question_id
        _active_confirm_queue = result_queue

    # Publish policy.ask event for WebSocket clients and logs
    _events.publish(
        "policy.ask",
        f"Confirmation required: {prompt_text}",
        {"question_id": question_id, "prompt": prompt_text},
    )

    # 2. Hook voice utterance callback during confirmation window
    previous_callback = voice_manager.on_utterance if voice_manager else None

    async def _voice_confirm_hook(utterance: str, speech_end_time: float) -> None:
        result_queue.put(("voice", utterance))

    if voice_manager:
        voice_manager.on_utterance = _voice_confirm_hook

    # 3. Spawn stdin reader thread
    def _terminal_input_worker() -> None:
        try:
            ans = input(f"\n[Policy Confirmation] {prompt_text} (yes/no, {timeout_seconds:.0f}s timeout): ").strip()
            result_queue.put(("typed", ans))
        except Exception:
            pass

    input_thread = threading.Thread(target=_terminal_input_worker, daemon=True)
    input_thread.start()

    # 4. Wait for first response or 10-second timeout
    approved = False
    try:
        source, answer = result_queue.get(timeout=timeout_seconds)
        norm = normalize_utterance(answer)
        # Only clear 'yes' counts
        if norm in ("yes", "y"):
            approved = True
            logger.info("Confirmation granted via %s: '%s'", source, answer)
        else:
            approved = False
            logger.info("Confirmation declined via %s: '%s'", source, answer)
    except queue.Empty:
        print(f"\n[Kira]: Confirmation timed out ({timeout_seconds:.0f}s). No answer means no.\n")
        approved = False
    finally:
        with _active_confirm_lock:
            _active_confirm_question_id = None
            _active_confirm_queue = None
        if voice_manager:
            voice_manager.on_utterance = previous_callback

    return approved


async def async_shared_confirm(
    prompt_text: str,
    voice_manager: Optional[VoiceManager] = None,
    timeout_seconds: float = 10.0,
    user_queue: Optional[asyncio.Queue] = None,
) -> bool:
    """
    Non-blocking async confirmation gate for running within the REPL asyncio loop.
    Awaits read-back TTS cleanly, listens to the unified user_queue and external WebSocket,
    and returns True on 'yes'/'y' or False on 'no'/timeout.
    """
    global _active_confirm_question_id, _active_confirm_queue
    question_id = uuid.uuid4().hex[:8]

    # 1. Read-back narration (TTS) before starting timer
    read_back = f"Confirmation required. {prompt_text}. Do you approve? Say or type yes to proceed."
    if voice_manager and not voice_manager.is_muted_voice:
        try:
            await voice_manager.speak(read_back)
        except Exception as exc:
            logger.debug("Read-back TTS error: %s", exc)

    result_queue: queue.Queue[tuple[str, str]] = queue.Queue()

    with _active_confirm_lock:
        _active_confirm_question_id = question_id
        _active_confirm_queue = result_queue

    # Publish policy.ask event for WebSocket clients and logs
    _events.publish(
        "policy.ask",
        f"Confirmation required: {prompt_text}",
        {"question_id": question_id, "prompt": prompt_text},
    )

    print(f"\n[Policy Confirmation] {prompt_text} (yes/no, {timeout_seconds:.0f}s timeout): ", flush=True)

    previous_callback = voice_manager.on_utterance if voice_manager else None

    async def _voice_confirm_hook(utterance: str, speech_end_time: float) -> None:
        result_queue.put(("voice", utterance))

    if voice_manager:
        voice_manager.on_utterance = _voice_confirm_hook

    approved = False
    start_time = time.monotonic()

    try:
        while (time.monotonic() - start_time) < timeout_seconds:
            # 1. Check external (WebSocket) response queue
            try:
                source, answer = result_queue.get_nowait()
                norm = normalize_utterance(answer)
                if norm in ("yes", "y"):
                    approved = True
                    logger.info("Confirmation granted via %s: '%s'", source, answer)
                else:
                    approved = False
                    logger.info("Confirmation declined via %s: '%s'", source, answer)
                break
            except queue.Empty:
                pass

            # 2. Check unified user_queue (handles typed input and push-to-talk voice)
            if user_queue:
                try:
                    source, answer, _ = user_queue.get_nowait()
                    norm = normalize_utterance(answer)
                    if norm in ("yes", "y"):
                        approved = True
                        logger.info("Confirmation granted via %s: '%s'", source, answer)
                    else:
                        approved = False
                        logger.info("Confirmation declined via %s: '%s'", source, answer)
                    break
                except asyncio.QueueEmpty:
                    pass

            await asyncio.sleep(0.05)
        else:
            print(f"\n[Kira]: Confirmation timed out ({timeout_seconds:.0f}s). No answer means no.\n", flush=True)
            approved = False

    finally:
        with _active_confirm_lock:
            _active_confirm_question_id = None
            _active_confirm_queue = None
        if voice_manager:
            voice_manager.on_utterance = previous_callback

    return approved


def run_voice_diagnostic(config: dict) -> None:
    """
    Interactive diagnostic tool to test:
    1. Speaker & Edge TTS playback
    2. Microphone capture & volume levels
    3. Push-to-Talk hotkey detection (F9 / Fn+F9)
    """
    print("\n" + "=" * 60)
    print("  Kira Voice Diagnostic & Self-Test")
    print("=" * 60)

    # 1. Audio Devices
    print("\n[Step 1/4] Checking Audio Devices...")
    try:
        in_dev = sd.query_devices(kind="input")
        out_dev = sd.query_devices(kind="output")
        print(f"  Default Input  (Mic):     {in_dev.get('name', 'Unknown')}")
        print(f"  Default Output (Speaker): {out_dev.get('name', 'Unknown')}")
    except Exception as exc:
        print(f"  Could not query default devices: {exc}")

    # 2. Speaker Test (Edge TTS)
    print("\n[Step 2/4] Testing Voice Output (Speakers / Headphones)...")
    print("  Synthesizing test greeting via Edge TTS and playing audio...")
    tts = TTSEngine(config)
    try:
        asyncio.run(
            tts.speak(
                "Hello! This is Kira. If you can hear this audio, your text to speech output is working properly."
            )
        )
        print("  -> Audio playback finished. Did you hear Kira speaking?")
    except Exception as exc:
        print(f"  -> Audio playback error: {exc}")

    # 3. Microphone & STT Test
    print("\n[Step 3/4] Testing Microphone & Speech Recognition...")
    print("  Recording 3 seconds from your microphone now. Speak something (e.g. 'Hello Kira')...")
    time.sleep(0.5)
    try:
        sample_rate = 16000
        duration = 3.0
        rec = sd.rec(int(sample_rate * duration), samplerate=sample_rate, channels=1, dtype="int16")
        sd.wait()
        max_amp = float(np.max(np.abs(rec)))
        print(f"  Captured audio. Max Amplitude: {max_amp:.0f} (typical speech: >1000)")
        if max_amp < 300:
            print("  [Warning]: Audio signal is very low. Check your Windows microphone volume or mute switch!")

        print("  Transcribing audio via Groq Whisper...")
        stt = STTEngine(config)
        audio_flat = rec.flatten()
        result = asyncio.run(stt.transcribe(audio_flat, duration))
        if result and result.text.strip():
            print(f"  -> Successfully recognized: \"{result.text}\" (Confidence: logprob={result.avg_logprob:.2f})")
        else:
            print("  -> No clear speech recognized. If you spoke, check microphone volume.")
    except Exception as exc:
        print(f"  -> Microphone error: {exc}")

    # 4. Hotkey Test
    print("\n[Step 4/4] Testing Hotkey Listener...")
    hotkey_name = config.get("voice", {}).get("hotkey", "F9")
    target_key = parse_hotkey_spec(hotkey_name)
    hint = f" (or [Fn+{hotkey_name}] on laptops with media keys)" if hotkey_name.lower().startswith("f") else ""
    print(f"  Press and release [{hotkey_name}] now{hint}...")
    print("  (Listening for 8 seconds, press Ctrl+C to skip)...")

    detected_key = None
    stop_event = threading.Event()

    def on_press(key: Any) -> None:
        nonlocal detected_key
        detected_key = key
        stop_event.set()

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    stop_event.wait(timeout=8.0)
    listener.stop()

    if detected_key:
        print(f"  -> KEY DETECTED! Key captured: {detected_key}")
        matched = False
        if detected_key == target_key:
            matched = True
        elif getattr(detected_key, "char", None) and getattr(target_key, "char", None):
            if detected_key.char.lower() == target_key.char.lower():
                matched = True
        elif getattr(target_key, "char", None) in ("`", "~") and getattr(detected_key, "vk", None) == 192:
            matched = True
        elif str(detected_key).lower() == f"key.{hotkey_name.lower()}":
            matched = True

        if matched:
            print(f"  -> Push-to-Talk [{hotkey_name}] matches perfectly!")
        else:
            print(f"  -> Note: Key pressed was {detected_key}. To use this key, update 'hotkey' in config.yaml.")
    else:
        print(f"  -> No key detected within 8s. Check your keyboard or press [{hotkey_name}] firmly.")

    print("\n" + "=" * 60)
    print("  Diagnostic complete! Run 'python -m backend.brain' to start.")
    print("=" * 60 + "\n")


def main() -> None:
    """CLI entrypoint for backend.voice."""
    import yaml
    from pathlib import Path

    if "-h" in sys.argv or "--help" in sys.argv:
        print("Usage:")
        print("  python -m backend.voice           Run voice listener REPL")
        print("  python -m backend.voice --test    Run voice, mic and hotkey diagnostic test")
        print("  python -m backend.voice --prepare Pre-download faster-whisper small model")
        sys.exit(0)

    config_path = Path(__file__).parent.parent / "config.yaml"
    cfg = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

    if "--test" in sys.argv:
        run_voice_diagnostic(cfg)
        sys.exit(0)

    if "--prepare" in sys.argv:
        local_model = cfg.get("voice", {}).get("stt", {}).get("local_model", "small")
        success = prepare_local_model(local_model)
        sys.exit(0 if success else 1)

    hotkey_display = cfg.get("voice", {}).get("hotkey", "F9")
    kill_display = cfg.get("voice", {}).get("kill_hotkey", "ctrl+alt+k")
    print("=" * 54)
    print(f"  Kira Voice Engine (Push-to-Talk: [{hotkey_display}] | Kill: [{kill_display}])")
    print("=" * 54)

    vm = VoiceManager(cfg)
    vm.print_privacy_notice()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    vm.start_hotkey_listener(loop)

    print(f"Hold [{hotkey_display}] to speak, release to send. Press Ctrl+C to exit.")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("\nExiting voice engine.")
    finally:
        vm.stop_hotkey_listener()
        loop.close()


if __name__ == "__main__":
    main()
