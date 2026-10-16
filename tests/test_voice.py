"""
test_voice.py
=============
Unit tests for Kira Step 4 Voice Pipeline (Ears & Mouth).

Covers:
- Text cleaning (code block replacement, markdown stripping, URLs)
- Sentence splitting (decimals 3.14, e.g., i.e., vs.)
- STT cooldown tracking (independent of LLM cooldowns)
- Audio duration gating (<0.3s ignored, capped at 30s)
- Groq STT verbose_json and faster-whisper fallback
- Confidence scoring and silence hallucination filtering
- Voice cannot enter Free mode (must be typed)
- "restricted mode" voice command works
- Stop-word exact match ("stop" / "kira stop" only; not "don't stop")
- Emergency kill hotkey (Ctrl+Alt+K)
- Edge TTS miniaudio decoding and pyttsx3 fallback
- Shared confirmation gate (typed yes, spoken yes, timeout means no)
- Zero audio files left on disk
- --prepare CLI and missing-model warning
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import glob
import os
import queue
import tempfile
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from openai import RateLimitError

from backend.policy import KILL_SWITCH, Mode, ModeManager
from backend.voice import (
    SILENCE_HALLUCINATIONS,
    STTCooldownTracker,
    STTEngine,
    TTSEngine,
    TranscriptResult,
    VoiceManager,
    audio_to_wav_bytes,
    check_local_model_available,
    clean_spoken_text,
    normalize_utterance,
    parse_hotkey_spec,
    prepare_local_model,
    shared_confirm,
    split_into_sentences,
)


@pytest.fixture(autouse=True)
def reset_kill_switch():
    KILL_SWITCH.reset()
    yield
    KILL_SWITCH.reset()


@pytest.fixture
def voice_config():
    return {
        "cooldowns": {
            "rate_limit": 900,
            "quota": 3600,
            "timeout": 60,
            "server_error": 60,
        },
        "voice": {
            "hotkey": "F9",
            "kill_hotkey": "ctrl+alt+k",
            "stt": {
                "groq_model": "whisper-large-v3-turbo",
                "local_model": "small",
                "min_duration": 0.3,
                "max_duration": 30.0,
                "min_avg_logprob": -1.0,
                "max_no_speech_prob": 0.6,
                "timeouts": {"groq": 10.0, "local": 30.0},
            },
            "tts": {
                "edge_voice": "en-US-GuyNeural",
                "edge_rate": "+0%",
                "backup_engine": "pyttsx3",
            },
            "safety": {
                "confirmation_timeout": 0.5,  # Short timeout for fast tests
                "privacy_notice": "Audio may be sent to Groq for speech recognition.",
            },
        },
    }


# =============================================================================
# 1. Spoken text cleaning and sentence splitting
# =============================================================================

def test_clean_spoken_text_replaces_code_blocks():
    text = (
        "Here is the function:\n"
        "```python\n"
        "def hello():\n"
        "    return 'world'\n"
        "```\n"
        "You can run it directly."
    )
    cleaned = clean_spoken_text(text)
    assert "The code is displayed on your screen." in cleaned
    assert "def hello():" not in cleaned
    assert "world" not in cleaned


def test_clean_spoken_text_strips_markdown_and_urls():
    text = "# Header\nCheck `print('hi')` and **bold** text at https://example.com/api?id=123"
    cleaned = clean_spoken_text(text)
    assert "#" not in cleaned
    assert "`" not in cleaned
    assert "**" not in cleaned
    assert "https://" not in cleaned
    assert "the link" in cleaned
    assert "bold text" in cleaned


def test_split_into_sentences_preserves_decimals_and_abbreviations():
    text = "The value of pi is 3.14, e.g. for geometry. Another sentence follows! Is it true? Yes, etc. is fine."
    sentences = split_into_sentences(text)
    assert len(sentences) == 4
    assert sentences[0] == "The value of pi is 3.14, e.g. for geometry."
    assert sentences[1] == "Another sentence follows!"
    assert sentences[2] == "Is it true?"
    assert sentences[3] == "Yes, etc. is fine."


# =============================================================================
# 2. STT Cooldown Tracker
# =============================================================================

def test_stt_cooldown_tracker_isolation():
    tracker = STTCooldownTracker({"rate_limit": 60, "quota": 300, "timeout": 10})
    assert tracker.is_available("groq")

    tracker.cooldown("groq", "rate_limit")
    assert not tracker.is_available("groq")

    tracker.disable("groq")
    assert not tracker.is_available("groq")

    tracker.reset()
    assert tracker.is_available("groq")


# =============================================================================
# 3. Audio Duration Gating and In-Memory Processing
# =============================================================================

@pytest.mark.asyncio
async def test_stt_short_audio_tap_ignored(voice_config):
    engine = STTEngine(voice_config)
    audio = np.zeros(1600, dtype=np.int16)
    # 0.1s is below 0.3s threshold
    result = await engine.transcribe(audio, duration=0.1)
    assert result is None


def test_audio_to_wav_bytes_creates_no_disk_files():
    audio = np.zeros(16000, dtype=np.int16)
    wav_bytes = audio_to_wav_bytes(audio)
    assert isinstance(wav_bytes, bytes)
    assert len(wav_bytes) > 0
    assert wav_bytes.startswith(b"RIFF")


# =============================================================================
# 4. Groq STT and Local Fallback
# =============================================================================

@pytest.mark.asyncio
async def test_stt_groq_success(voice_config):
    engine = STTEngine(voice_config)
    audio = np.zeros(16000, dtype=np.int16)

    mock_response = MagicMock()
    mock_response.text = "Hello Kira"
    mock_response.segments = [
        {"avg_logprob": -0.2, "no_speech_prob": 0.05, "text": "Hello Kira"}
    ]

    with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
        with patch("backend.voice.AsyncOpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_response)
            mock_openai_cls.return_value = mock_client

            result = await engine.transcribe(audio, duration=1.0)

            assert result is not None
            assert result.text == "Hello Kira"
            assert result.source == "groq"
            assert result.is_valid
            assert result.avg_logprob == pytest.approx(-0.2)
            assert result.no_speech_prob == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_stt_groq_failure_falls_back_to_local(voice_config):
    engine = STTEngine(voice_config)
    audio = np.zeros(16000, dtype=np.int16)

    # Mock local transcription output
    mock_local_result = TranscriptResult(
        text="Local recognized text",
        avg_logprob=-0.3,
        no_speech_prob=0.1,
        source="local",
        duration=1.0,
    )

    with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
        with patch("backend.voice.AsyncOpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create = AsyncMock(
                side_effect=RateLimitError("429 Rate limit", response=MagicMock(status_code=429), body={})
            )
            mock_openai_cls.return_value = mock_client

            with patch.object(engine, "_transcribe_local", new_callable=AsyncMock) as mock_local:
                mock_local.return_value = mock_local_result

                result = await engine.transcribe(audio, duration=1.0)

                assert result is not None
                assert result.text == "Local recognized text"
                assert result.source == "local"
                # Check that Groq was put into cooldown
                assert not engine.cooldown_tracker.is_available("groq")


# =============================================================================
# 5. Quality metrics: Confidence and Hallucination Filter
# =============================================================================

@pytest.mark.asyncio
async def test_stt_low_confidence_flagged(voice_config):
    engine = STTEngine(voice_config)
    audio = np.zeros(16000, dtype=np.int16)

    mock_response = MagicMock()
    mock_response.text = "Muffled speech"
    # avg_logprob -1.5 is below min threshold -1.0
    mock_response.segments = [
        {"avg_logprob": -1.5, "no_speech_prob": 0.1, "text": "Muffled speech"}
    ]

    with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
        with patch("backend.voice.AsyncOpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_response)
            mock_openai_cls.return_value = mock_client

            result = await engine.transcribe(audio, duration=1.0)
            assert result is not None
            assert result.low_confidence is True
            assert not result.is_valid


@pytest.mark.asyncio
async def test_stt_silence_hallucination_filtered(voice_config):
    engine = STTEngine(voice_config)
    audio = np.zeros(16000, dtype=np.int16)

    mock_response = MagicMock()
    mock_response.text = "Thank you."
    mock_response.segments = [
        {"avg_logprob": -0.1, "no_speech_prob": 0.05, "text": "Thank you."}
    ]

    with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
        with patch("backend.voice.AsyncOpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_response)
            mock_openai_cls.return_value = mock_client

            result = await engine.transcribe(audio, duration=1.0)
            assert result is not None
            assert result.hallucination is True
            assert not result.is_valid


# =============================================================================
# 6. Safety: Free Mode, Restricted Mode, and Stop Word
# =============================================================================

@pytest.mark.asyncio
async def test_spoken_free_mode_refused(voice_config):
    mode_mgr = ModeManager()
    vm = VoiceManager(voice_config, mode_manager=mode_mgr)

    # Mock STT to return "free mode"
    vm.stt.transcribe = AsyncMock(return_value=TranscriptResult(text="Free mode", source="groq", duration=1.0))
    vm.speak = AsyncMock()

    dummy_audio = np.zeros(16000, dtype=np.int16)
    result = await vm.handle_recorded_audio(dummy_audio, 1.0, time.monotonic())

    assert result is None
    assert mode_mgr.mode == Mode.RESTRICTED
    # Spoke refusal
    vm.speak.assert_called_once()
    assert "Free mode cannot be enabled by voice" in vm.speak.call_args[0][0]


@pytest.mark.asyncio
async def test_spoken_restricted_mode_accepted(voice_config):
    mode_mgr = ModeManager(confirm_callback=lambda: True)
    mode_mgr.enter_free()
    assert mode_mgr.mode == Mode.FREE

    vm = VoiceManager(voice_config, mode_manager=mode_mgr)
    vm.stt.transcribe = AsyncMock(return_value=TranscriptResult(text="restricted mode", source="groq", duration=1.0))
    vm.speak = AsyncMock()

    dummy_audio = np.zeros(16000, dtype=np.int16)
    await vm.handle_recorded_audio(dummy_audio, 1.0, time.monotonic())

    assert mode_mgr.mode == Mode.RESTRICTED
    vm.speak.assert_called_once()
    assert "Switched to Restricted mode" in vm.speak.call_args[0][0]


@pytest.mark.asyncio
async def test_stop_word_exact_match_only(voice_config):
    vm = VoiceManager(voice_config)
    vm.speak = AsyncMock()
    dummy_audio = np.zeros(16000, dtype=np.int16)

    # 1. "stop" -> triggers kill switch
    vm.stt.transcribe = AsyncMock(return_value=TranscriptResult(text="stop", source="groq", duration=1.0))
    await vm.handle_recorded_audio(dummy_audio, 1.0, time.monotonic())
    assert KILL_SWITCH.is_stopped()

    # Reset
    KILL_SWITCH.reset()

    # 2. "Kira stop" -> triggers kill switch
    vm.stt.transcribe = AsyncMock(return_value=TranscriptResult(text="Kira stop!", source="groq", duration=1.0))
    await vm.handle_recorded_audio(dummy_audio, 1.0, time.monotonic())
    assert KILL_SWITCH.is_stopped()

    # Reset
    KILL_SWITCH.reset()

    # 3. "don't stop" -> does NOT trigger kill switch
    vm.stt.transcribe = AsyncMock(return_value=TranscriptResult(text="don't stop", source="groq", duration=1.0))
    await vm.handle_recorded_audio(dummy_audio, 1.0, time.monotonic())
    assert not KILL_SWITCH.is_stopped()

    # 4. "stop the car" -> does NOT trigger kill switch
    vm.stt.transcribe = AsyncMock(return_value=TranscriptResult(text="stop the car", source="groq", duration=1.0))
    await vm.handle_recorded_audio(dummy_audio, 1.0, time.monotonic())
    assert not KILL_SWITCH.is_stopped()


def test_emergency_kill_hotkey(voice_config):
    vm = VoiceManager(voice_config)
    vm.tts.stop = MagicMock()

    # Simulate Ctrl+Alt+K keypress sequence
    from pynput import keyboard

    # 1. Press ctrl
    vm._on_press(keyboard.Key.ctrl_l)
    assert vm._ctrl_down
    assert not KILL_SWITCH.is_stopped()

    # 2. Press alt
    vm._on_press(keyboard.Key.alt_l)
    assert vm._alt_down
    assert not KILL_SWITCH.is_stopped()

    # 3. Press 'k'
    k_key = keyboard.KeyCode.from_char("k")
    vm._on_press(k_key)
    assert KILL_SWITCH.is_stopped()
    vm.tts.stop.assert_called_once()


def test_parse_hotkey_spec_aliases():
    from pynput import keyboard

    assert parse_hotkey_spec("F9") == keyboard.Key.f9
    assert parse_hotkey_spec("`") == keyboard.KeyCode.from_char("`")
    assert parse_hotkey_spec("backtick") == keyboard.KeyCode.from_char("`")
    assert parse_hotkey_spec("grave") == keyboard.KeyCode.from_char("`")
    assert parse_hotkey_spec("tilde") == keyboard.KeyCode.from_char("~")
    assert parse_hotkey_spec("~") == keyboard.KeyCode.from_char("~")


def test_voice_manager_backtick_hotkey(voice_config):
    from pynput import keyboard

    cfg = dict(voice_config)
    cfg["voice"] = dict(voice_config["voice"])
    cfg["voice"]["hotkey"] = "`"

    vm = VoiceManager(cfg)
    assert getattr(vm.talk_key, "char", None) == "`"

    # Direct KeyCode match
    assert vm._is_talk_key(keyboard.KeyCode.from_char("`"))
    # KeyCode with VK 192 (Windows OEM_3)
    assert vm._is_talk_key(keyboard.KeyCode(vk=192, char="`"))
    assert vm._is_talk_key(keyboard.KeyCode.from_vk(192))
    # Non-matching key
    assert not vm._is_talk_key(keyboard.KeyCode.from_char("a"))
    assert not vm._is_talk_key(keyboard.Key.f9)

    # Press backtick starts recording
    with patch.object(vm, "_start_recording") as mock_start, patch.object(vm, "stop_speech") as mock_stop:
        vm._on_press(keyboard.KeyCode.from_char("`"))
        assert vm._talk_key_held is True
        mock_start.assert_called_once()
        mock_stop.assert_called_once()

    # Release backtick stops recording
    vm._is_recording = True
    with patch.object(vm, "_stop_recording", return_value=(None, 0.1, time.monotonic())) as mock_stop_rec:
        vm._on_release(keyboard.KeyCode.from_char("`"))
        assert vm._talk_key_held is False
        mock_stop_rec.assert_called_once()


# =============================================================================
# 7. Text-to-Speech (TTS): Edge TTS and pyttsx3 fallback
# =============================================================================

@pytest.mark.asyncio
async def test_tts_speak_pipeline_edge_success(voice_config):
    tts = TTSEngine(voice_config)
    mock_samples = np.zeros(24000, dtype=np.int16)

    tts.synthesize_sentence_edge = AsyncMock(return_value=(mock_samples, 24000))
    tts.play_pcm = AsyncMock()

    await tts.speak("First sentence. Second sentence.", speech_end_time=time.monotonic())

    assert tts.synthesize_sentence_edge.call_count == 2
    assert tts.play_pcm.call_count == 2


@pytest.mark.asyncio
async def test_tts_fallback_to_pyttsx3_on_edge_failure(voice_config):
    tts = TTSEngine(voice_config)

    tts.synthesize_sentence_edge = AsyncMock(return_value=None)
    tts._speak_pyttsx3_sync = MagicMock()

    await tts.speak("A test sentence for fallback.")

    tts._speak_pyttsx3_sync.assert_called_once()
    assert tts._speak_pyttsx3_sync.call_args[0][0] == "A test sentence for fallback."


# =============================================================================
# 8. Shared Confirmation Gate: Spoken, Typed, and Timeout
# =============================================================================

def test_shared_confirm_typed_yes(voice_config):
    vm = VoiceManager(voice_config)
    vm.speak = AsyncMock()

    with patch("builtins.input", return_value="yes"):
        approved = shared_confirm("Run risky command?", voice_manager=vm, timeout_seconds=1.0)
        assert approved is True


def test_shared_confirm_typed_no(voice_config):
    vm = VoiceManager(voice_config)
    vm.speak = AsyncMock()

    with patch("builtins.input", return_value="no"):
        approved = shared_confirm("Run risky command?", voice_manager=vm, timeout_seconds=1.0)
        assert approved is False


def test_shared_confirm_timeout_means_no(voice_config):
    vm = VoiceManager(voice_config)
    vm.speak = AsyncMock()

    def hang_input(*args, **kwargs):
        time.sleep(2.0)
        return "yes"

    with patch("builtins.input", side_effect=hang_input):
        approved = shared_confirm("Run risky command?", voice_manager=vm, timeout_seconds=0.2)
        assert approved is False


def test_shared_confirm_spoken_yes(voice_config):
    vm = VoiceManager(voice_config)
    vm.speak = AsyncMock()

    # Simulate speech arriving before typed input
    def simulate_spoken(prompt, voice_mgr, timeout_sec):
        # Trigger the hook directly
        time.sleep(0.05)
        if voice_mgr.on_utterance:
            asyncio.run(voice_mgr.on_utterance("yes", time.monotonic()))
        return "timeout"

    # Block terminal input so voice wins
    with patch("builtins.input", side_effect=lambda *args: time.sleep(2.0)):
        # Start voice trigger thread
        t = threading.Thread(
            target=lambda: (time.sleep(0.05), asyncio.run(vm.on_utterance("yes", time.monotonic())) if vm.on_utterance else None)
        )
        t.start()
        approved = shared_confirm("Run risky command?", voice_manager=vm, timeout_seconds=1.0)
        t.join()
        assert approved is True


# =============================================================================
# 9. Model Preparation and Missing-Model Warning
# =============================================================================

def test_prepare_local_model_downloads():
    with patch("faster_whisper.download_model", return_value="D:/cache/small") as mock_dl:
        success = prepare_local_model("small")
        assert success is True
        mock_dl.assert_called_once_with("small", local_files_only=False)


def test_check_local_model_available():
    with patch("faster_whisper.download_model", side_effect=Exception("Not found")):
        assert not check_local_model_available("small")

    with patch("faster_whisper.download_model", return_value="path"):
        assert check_local_model_available("small")


# =============================================================================
# 10. Zero Audio Files Left on Disk
# =============================================================================

def test_no_audio_files_created_in_project():
    audio_patterns = ["*.wav", "*.mp3", "*.ogg", "*.flac"]
    for pattern in audio_patterns:
        matches = glob.glob(f"backend/**/{pattern}", recursive=True) + glob.glob(f"workspace/**/{pattern}", recursive=True)
        assert len(matches) == 0, f"Found audio file on disk: {matches}"
