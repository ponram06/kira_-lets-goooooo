"""
test_persona_polish.py
======================
M9: per-persona voice applied live, greeting, honest friendly errors that
never kill the chat loop, and that both personas share the same safety rules
(tone lives in the prompt, rules live in code).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app_state import AppState
from backend.brain import run_agent_safely
from backend.config_loader import load_yaml


class FakeTTS:
    edge_voice = "en-US-GuyNeural"


class FakeVoice:
    def __init__(self):
        self.tts = FakeTTS()
        self.on_ptt_press = None


CFG = {"persona": {"name": "kira", "available_personas": {
    "kira": {"name": "kira", "greeting": "Greetings.", "system_prompt": "k",
             "character": {"voice": "en-US-GuyNeural"}},
    "ryuk": {"name": "ryuk", "greeting": "Got an apple?", "system_prompt": "r",
             "character": {"voice": "en-US-ChristopherNeural"},
             "error_lines": {"crash": "Well, that broke."}},
    "bad": {"name": "bad", "system_prompt": "b", "character": {"voice": "x; rm -rf /"}},
}}}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRA_USER_DIR", str(tmp_path))
    return AppState(CFG, voice_manager=FakeVoice())


def test_voice_switches_with_persona(state):
    assert state.voice_manager.tts.edge_voice == "en-US-GuyNeural"
    assert state.set_persona("ryuk")
    assert state.voice_manager.tts.edge_voice == "en-US-ChristopherNeural"


def test_invalid_voice_name_ignored(state):
    state.set_persona("ryuk")
    state.set_persona("bad")
    assert state.voice_manager.tts.edge_voice == "en-US-ChristopherNeural"


def test_greeting_and_error_lines(state):
    assert state.greeting == "Greetings."
    assert "stopped safely" in state.friendly_error("crash")
    state.set_persona("ryuk")
    assert state.greeting == "Got an apple?"
    assert state.friendly_error("crash") == "Well, that broke."
    assert "Ollama" in state.friendly_error("offline")      # default when persona has none


@pytest.mark.asyncio
async def test_agent_crash_becomes_spoken_message(state):
    agent = MagicMock()
    agent.run = AsyncMock(side_effect=RuntimeError("kaboom"))
    reply, provider = await run_agent_safely(agent, [], state)
    assert provider is None and "stopped safely" in reply
    assert "kaboom" not in reply


@pytest.mark.asyncio
async def test_no_provider_becomes_offline_message(state):
    agent = MagicMock()
    agent.run = AsyncMock(return_value=("All providers unavailable.", None))
    reply, provider = await run_agent_safely(agent, [], state)
    assert provider is None and "language model" in reply


@pytest.mark.asyncio
async def test_normal_reply_untouched(state):
    agent = MagicMock()
    agent.run = AsyncMock(return_value=("Hello, World!", "groq"))
    assert await run_agent_safely(agent, [], state) == ("Hello, World!", "groq")


def test_real_personas_differ_in_tone_not_rules():
    cfg = load_yaml(Path(__file__).resolve().parents[1] / "config.yaml")
    p = cfg["persona"]["available_personas"]
    assert "calm" in p["kira"]["system_prompt"].lower()
    assert "apple" in p["ryuk"]["system_prompt"].lower() and "sarcastic" in p["ryuk"]["system_prompt"].lower()
    for name in ("kira", "ryuk"):
        assert p[name].get("greeting")
        # No persona may carry permission settings
        for key in ("mode", "allowlisted", "approved", "free_mode", "policy"):
            assert key not in p[name], f"{name} must not configure {key}"
