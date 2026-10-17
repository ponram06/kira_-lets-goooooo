"""
M1 app controls — backend side.

- AppState: persona switch (live + saved), mic mute vs sleep, push-to-talk
  wakes from sleep, settings reload, shutdown, mode never saved.
- WebSocket control commands: free_mode_request (PIN gate, lockout, never
  echoed), set_persona, set_mic_muted, set_sleep, get_status, shutdown.
- brain.handle_local_command: Free mode only via the avatar PIN dialog.
"""

import asyncio
import json
import socket
import time
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

import backend.events as events_module
from backend import user_config
from backend.app_state import AppState
from backend.brain import FREE_DISABLED, FREE_HOWTO, handle_local_command
from backend.policy import KILL_SWITCH, Mode, ModeManager
from backend.server import WebSocketServer
from tests.conftest import TEST_PIN

CFG = {
    "persona": {
        "name": "kira",
        "system_prompt": "fallback",
        "available_personas": {
            "kira": {"name": "kira", "system_prompt": "You are Kira."},
            "ryuk": {"name": "ryuk", "system_prompt": "You are Ryuk."},
        },
    }
}


class FakeVoice:
    def __init__(self):
        self.is_muted_mic = False
        self.on_ptt_press = None
        self.stopped = False

    def mute_mic(self, muted=True):
        self.is_muted_mic = muted

    def stop_speech(self):
        self.stopped = True


@pytest.fixture
def captured_events():
    seen = []
    events_module.subscribe(seen.append)
    yield seen
    events_module.unsubscribe(seen.append)


@pytest.fixture(autouse=True)
def _reset_kill():
    KILL_SWITCH.reset()
    yield
    KILL_SWITCH.reset()


# ── AppState ────────────────────────────────────────────────────────────────

def test_persona_switch_live_and_saved(captured_events):
    st = AppState(CFG, voice_manager=FakeVoice())
    assert st.persona == "kira" and st.system_prompt == "You are Kira."
    assert st.set_persona("Ryuk") is True
    assert st.system_prompt == "You are Ryuk."
    assert user_config.load_settings()["persona"] == "ryuk"
    assert any(e["type"] == "persona.change" and e["data"]["persona"] == "ryuk" for e in captured_events)


def test_unknown_persona_refused():
    st = AppState(CFG, voice_manager=FakeVoice())
    assert st.set_persona("light") is False
    assert st.persona == "kira"


def test_mute_is_separate_from_sleep(captured_events):
    voice = FakeVoice()
    st = AppState(CFG, voice_manager=voice)
    st.set_sleeping(True)
    assert st.sleeping is True
    assert voice.is_muted_mic is False          # sleep never mutes the mic
    st.set_mic_muted(True)
    assert voice.is_muted_mic is True and st.sleeping is True
    assert user_config.load_settings()["mic_muted"] is True
    types = [e["type"] for e in captured_events]
    assert "sleep.change" in types and "mic.mute" in types


def test_push_to_talk_wakes_from_sleep():
    voice = FakeVoice()
    st = AppState(CFG, voice_manager=voice)
    st.set_sleeping(True)
    voice.on_ptt_press()                         # VoiceManager calls this on PTT press
    assert st.sleeping is False


def test_sleep_not_saved_and_mode_never_saved():
    st = AppState(CFG, voice_manager=FakeVoice())
    st.set_sleeping(True)
    st.set_mic_muted(False)
    user_config.update_settings(mode="free")     # ignored key
    saved = user_config.load_settings()
    assert "sleeping" not in saved and "mode" not in saved


def test_reload_settings_applies_live_values():
    mm = ModeManager(pin=TEST_PIN)
    voice = FakeVoice()
    st = AppState(CFG, voice_manager=voice, mode_manager=mm)
    user_config.update_settings(persona="ryuk", mic_muted=True,
                                overrides={"mode.free_revert_timeout": 900})
    st.reload_settings()
    assert st.persona == "ryuk" and voice.is_muted_mic is True and mm.free_timeout == 900


def test_status_has_no_secrets():
    mm = ModeManager(pin=TEST_PIN)
    st = AppState(CFG, voice_manager=FakeVoice(), mode_manager=mm)
    status = st.status()
    assert status["free_mode_available"] is True
    assert TEST_PIN not in json.dumps(status)


async def test_shutdown_event():
    st = AppState(CFG, voice_manager=FakeVoice())
    st.bind_loop(asyncio.get_running_loop())
    st.request_shutdown()
    await asyncio.wait_for(st.wait_shutdown(), timeout=1)
    assert st.shutdown_requested


# ── brain local commands ────────────────────────────────────────────────────

def test_free_phrase_points_to_pin_dialog():
    voice = FakeVoice()
    assert handle_local_command("free mode", ModeManager(pin=TEST_PIN), voice) == FREE_HOWTO
    assert handle_local_command("free mode", ModeManager(), voice) == FREE_DISABLED


def test_typed_pin_never_enters_free_mode():
    mm = ModeManager(pin=TEST_PIN)
    assert handle_local_command(TEST_PIN, mm, FakeVoice()) is None   # goes to the LLM path, not the mode gate
    assert mm.mode == Mode.RESTRICTED


def test_restricted_and_stop_phrases():
    mm = ModeManager(pin=TEST_PIN)
    mm.enter_free(pin=TEST_PIN)
    voice = FakeVoice()
    assert "Restricted" in handle_local_command("restricted mode", mm, voice)
    assert mm.mode == Mode.RESTRICTED
    handle_local_command("stop", mm, voice)
    assert KILL_SWITCH.is_stopped() and voice.stopped


# ── WebSocket control commands ──────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def harness(tmp_path: Path):
    port = _free_port()
    token_file = tmp_path / ".kira_test" / "token"
    mm = ModeManager(pin=TEST_PIN, max_attempts=3, lockout_seconds=60)
    voice = FakeVoice()
    st = AppState(CFG, voice_manager=voice, mode_manager=mm)
    config = {"server": {"enabled": True, "host": "127.0.0.1", "port": port,
                         "token_file": str(token_file), "allowed_origins": ["null"]}}
    server = WebSocketServer(config=config, mode_manager=mm, voice_manager=voice, app_state=st)
    server.start_in_thread()
    deadline = time.monotonic() + 5
    while not server.running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.running
    yield {"uri": f"ws://127.0.0.1:{port}", "token": token_file.read_text().strip(),
           "mm": mm, "state": st, "voice": voice}
    server.stop()


async def _session(h):
    ws = await connect(h["uri"], additional_headers={"Origin": "null"})
    await ws.send(json.dumps({"type": "auth", "token": h["token"]}))
    assert json.loads(await ws.recv())["type"] == "auth_ok"
    snap = json.loads(await ws.recv())
    return ws, snap


async def _ack(ws, command):
    """Read frames until the command_ack for *command* arrives."""
    for _ in range(20):
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        if msg.get("type") == "command_ack" and msg.get("command") == command:
            return msg
    raise AssertionError("no ack")


async def test_snapshot_includes_controls(harness):
    ws, snap = await _session(harness)
    assert snap["persona"] == "kira" and snap["personas"] == ["kira", "ryuk"]
    assert snap["free_mode_available"] is True and snap["mic_muted"] is False
    await ws.close()


async def test_free_mode_request_ok_and_pin_not_echoed(harness):
    ws, _ = await _session(harness)
    await ws.send(json.dumps({"command": "free_mode_request", "pin": TEST_PIN, "request_id": "r1"}))
    raw = None
    for _ in range(20):
        raw = await asyncio.wait_for(ws.recv(), timeout=3)
        if '"command_ack"' in raw:
            break
    ack = json.loads(raw)
    assert ack["status"] == "ok" and ack["request_id"] == "r1"
    assert TEST_PIN not in raw
    assert harness["mm"].mode == Mode.FREE
    await ws.close()


async def test_free_mode_wrong_pin_then_lockout(harness):
    ws, _ = await _session(harness)
    statuses = []
    for pin in ("1111", "2222", "3333", TEST_PIN):
        await ws.send(json.dumps({"command": "free_mode_request", "pin": pin}))
        statuses.append((await _ack(ws, "free_mode_request"))["status"])
    assert statuses == ["wrong_pin", "wrong_pin", "locked", "locked"]
    assert harness["mm"].mode == Mode.RESTRICTED
    await ws.close()


async def test_free_mode_request_disabled_without_pin(harness):
    harness["mm"]._fixed_pin = ""          # simulate KIRA_FREE_MODE_PIN missing
    ws, _ = await _session(harness)
    await ws.send(json.dumps({"command": "free_mode_request", "pin": "1234"}))
    assert (await _ack(ws, "free_mode_request"))["status"] == "disabled"
    await ws.close()


async def test_bare_free_mode_command_still_refused(harness):
    ws, _ = await _session(harness)
    await ws.send(json.dumps({"command": "free_mode"}))
    msg = json.loads(await ws.recv())
    assert msg["code"] == "free_mode_forbidden"
    assert harness["mm"].mode == Mode.RESTRICTED
    await ws.close()


async def test_persona_mute_sleep_status_commands(harness):
    ws, _ = await _session(harness)
    await ws.send(json.dumps({"command": "set_persona", "persona": "ryuk"}))
    assert (await _ack(ws, "set_persona"))["status"] == "executed"
    await ws.send(json.dumps({"command": "set_persona", "persona": "nobody"}))
    assert (await _ack(ws, "set_persona"))["status"] == "invalid"
    await ws.send(json.dumps({"command": "set_mic_muted", "muted": True}))
    await _ack(ws, "set_mic_muted")
    await ws.send(json.dumps({"command": "set_sleep", "sleeping": True}))
    await _ack(ws, "set_sleep")
    await ws.send(json.dumps({"command": "get_status"}))
    snap = (await _ack(ws, "get_status"))["snapshot"]
    assert snap["persona"] == "ryuk" and snap["mic_muted"] is True and snap["sleeping"] is True
    await ws.close()


async def test_shutdown_command_requests_shutdown(harness):
    st = harness["state"]
    loop = asyncio.get_running_loop()
    st.bind_loop(loop)
    ws, _ = await _session(harness)
    await ws.send(json.dumps({"command": "shutdown"}))
    assert (await _ack(ws, "shutdown"))["status"] == "executed"
    await asyncio.wait_for(st.wait_shutdown(), timeout=2)
    await ws.close()


# ── M2: typed chat input, countdown cancel, forwarded events ────────────────

def test_submit_text_goes_to_sink():
    st = AppState(CFG, voice_manager=FakeVoice())
    assert st.submit_text("hello") is False          # no sink yet
    got = []
    st.set_text_sink(got.append)
    assert st.submit_text("  open   notepad ") is True
    assert got == ["open notepad"]
    assert st.submit_text("   ") is False


async def test_user_text_and_cancel_countdown_commands(harness):
    got = []
    harness["state"].set_text_sink(got.append)
    ws, snap = await _session(harness)
    assert "mic_available" in snap
    await ws.send(json.dumps({"command": "user_text", "text": "what time is it"}))
    assert (await _ack(ws, "user_text"))["status"] == "executed"
    await ws.send(json.dumps({"command": "user_text", "text": "x" * 3000}))
    assert (await _ack(ws, "user_text"))["status"] == "invalid"
    await ws.send(json.dumps({"command": "cancel_countdown"}))
    assert (await _ack(ws, "cancel_countdown"))["status"] == "executed"
    assert got == ["what time is it"]
    await ws.close()


def test_new_events_are_forwarded():
    server = WebSocketServer(config={"server": {"enabled": False, "token_file": ".kira/test_token2"}})
    sent = []

    class Q:
        def put_nowait(self, payload):
            sent.append(json.loads(payload))

    server._clients = {object(): Q()}
    server._process_and_broadcast({"type": "policy.countdown", "text": "", "data": {"label": "Sending to Sam", "seconds_left": 2}})
    server._process_and_broadcast({"type": "reminder.due", "text": "", "data": {"text": "tea", "kind": "timer", "missed": False}})
    server._process_and_broadcast({"type": "assistant.reply", "text": "", "data": {"text": "Hello"}})
    assert [m["type"] for m in sent] == ["countdown", "reminder", "reply"]
    assert sent[0]["seconds_left"] == 2 and sent[1]["text"] == "tea" and sent[2]["text"] == "Hello"
