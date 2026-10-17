"""
test_avatar_profiles.py
=======================
M8 backend side: character profiles in snapshots and persona events (no
prompts leak), emotion tags stripped from replies and forwarded, and the
real config.yaml profiles are valid.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import backend.events as events_module
from backend.app_state import AppState
from backend.brain import EMOTION_HINT, extract_emotion
from backend.config_loader import load_yaml
from backend.server import WebSocketServer

CFG = {
    "persona": {
        "name": "kira",
        "system_prompt": "fallback",
        "available_personas": {
            "kira": {"name": "kira", "system_prompt": "SECRET PROMPT TEXT",
                     "character": {"display_name": "Kira", "renderer": "vrm", "asset": "models/kira.vrm",
                                   "theme": "notebook", "framing": "bust", "voice": "en-US-GuyNeural"}},
            "ryuk": {"name": "ryuk", "system_prompt": "You are Ryuk."},
        },
    }
}


@pytest.fixture
def captured():
    seen = []
    events_module.subscribe(seen.append)
    yield seen
    events_module.unsubscribe(seen.append)


def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRA_USER_DIR", str(tmp_path))
    return AppState(CFG)


def test_profile_in_status_without_prompt(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch)
    snap = st.status()
    assert snap["character"] == {"id": "kira", "display_name": "Kira", "renderer": "vrm",
                                 "asset": "models/kira.vrm", "voice": "en-US-GuyNeural",
                                 "theme": "notebook", "framing": "bust"}
    assert "SECRET PROMPT TEXT" not in str(snap)


def test_profile_defaults_when_missing(tmp_path, monkeypatch):
    st = _state(tmp_path, monkeypatch)
    p = st.character_profile("ryuk")
    assert p["renderer"] == "sprite" and p["asset"] is None and p["display_name"] == "Ryuk"


def test_persona_change_event_carries_character(tmp_path, monkeypatch, captured):
    st = _state(tmp_path, monkeypatch)
    assert st.set_persona("ryuk")
    ev = [e for e in captured if e["type"] == "persona.change"][-1]
    assert ev["data"]["character"]["id"] == "ryuk"


def test_server_forwards_character_and_emotion(tmp_path):
    srv = WebSocketServer(config={"server": {"token_file": str(tmp_path / "tok")}})
    sent = []
    srv._broadcast = sent.append
    srv._process_and_broadcast({"type": "persona.change", "text": "", "timestamp": "t",
                                "data": {"persona": "kira", "character": {"id": "kira", "renderer": "vrm",
                                                                         "asset": "models/kira.vrm", "evil": "x"}}})
    srv._process_and_broadcast({"type": "assistant.emotion", "text": "", "timestamp": "t", "data": {"emotion": "happy"}})
    srv._process_and_broadcast({"type": "assistant.emotion", "text": "", "timestamp": "t", "data": {"emotion": "<script>"}})
    assert sent[0]["character"] == {"id": "kira", "renderer": "vrm", "asset": "models/kira.vrm"}
    assert (sent[1]["type"], sent[1]["emotion"]) == ("emotion", "happy")
    assert len(sent) == 2


@pytest.mark.parametrize("text,emotion,clean", [
    ("[emotion:happy] Done!", "happy", "Done!"),
    ("All good [mood:relaxed]", "relaxed", "All good"),
    ("[emotion:evil] hi [emotion:sad]", "sad", "hi"),
    ("no tag", None, "no tag"),
])
def test_extract_emotion(text, emotion, clean):
    assert extract_emotion(text) == (emotion, clean)


def test_emotion_hint_cannot_change_rules():
    assert "never your rules" in EMOTION_HINT


def test_real_config_profiles_are_valid():
    cfg = load_yaml(Path(__file__).resolve().parents[1] / "config.yaml")
    personas = cfg["persona"]["available_personas"]
    for name, p in personas.items():
        c = p.get("character") or {}
        assert c.get("renderer", "sprite") in ("sprite", "vrm"), name
        assert c.get("theme", "notebook") in ("notebook", "gotham"), name
        assert c.get("framing", "bust") in ("bust", "full"), name
        if c.get("asset"):
            assert str(c["asset"]).lower().endswith(".vrm")
            assert not Path(c["asset"]).is_absolute(), "keep model paths relative in the repo config"
