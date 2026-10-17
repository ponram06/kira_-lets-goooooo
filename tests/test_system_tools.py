"""
M2 system tools, reminders, countdown and the agent fixes. All hardware is
faked: no real volume, brightness, screen capture, session lock or audio.
"""

import asyncio
import json
import struct
import zlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.events as events_module
from backend import countdown, policy as policy_module, reminders
from backend.agent import AgentRunner
from backend.policy import KILL_SWITCH, Mode, ModeManager, RiskLevel, TOOL_REGISTRY, check
from backend.providers import ProviderManager
from backend.tools import KIRA_TOOLS_SCHEMA, TOOL_FUNCTIONS, reminders_tool, system_control
from tests.conftest import TEST_PIN


@pytest.fixture(autouse=True)
def _reset():
    KILL_SWITCH.reset()
    reminders.register_scheduler(None)
    yield
    KILL_SWITCH.reset()
    reminders.register_scheduler(None)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = (tmp_path / "workspace").resolve()
    ws.mkdir()
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", ws)
    return ws


@pytest.fixture
def captured():
    seen = []
    events_module.subscribe(seen.append)
    yield seen
    events_module.unsubscribe(seen.append)


# ── registry completeness (working rule 3) ──────────────────────────────────

def test_every_tool_is_registered_with_risk_and_flags():
    for name in TOOL_FUNCTIONS:
        entry = TOOL_REGISTRY.get(name)
        assert entry is not None, f"{name} missing from policy.TOOL_REGISTRY"
        assert isinstance(entry["risk"], RiskLevel)
        assert isinstance(entry["affects_people"], bool)
        assert isinstance(entry["outside_workspace"], bool)


def test_every_schema_has_an_implementation():
    names = [t["function"]["name"] for t in KIRA_TOOLS_SCHEMA]
    assert len(names) == len(set(names))
    assert set(names) == set(TOOL_FUNCTIONS)


@pytest.mark.parametrize("tool", ["volume", "brightness", "system_info", "set_timer",
                                  "set_reminder", "list_reminders", "cancel_reminder"])
def test_low_risk_tools_run_in_restricted(tool):
    assert check(tool, Mode.RESTRICTED) == "allow"


@pytest.mark.parametrize("tool", ["lock_pc", "take_screenshot"])
def test_lock_and_screenshot_ask_in_restricted_allow_in_free(tool):
    assert check(tool, Mode.RESTRICTED) == "ask"
    assert check(tool, Mode.FREE) == "allow"


def test_kill_switch_blocks_new_tools():
    KILL_SWITCH.stop()
    assert check("volume", Mode.FREE) == "block"


# ── volume ──────────────────────────────────────────────────────────────────

class FakeEndpoint:
    def __init__(self, level=0.5, muted=0):
        self.level, self.muted = level, muted

    def GetMasterVolumeLevelScalar(self): return self.level
    def SetMasterVolumeLevelScalar(self, v, ctx): self.level = v
    def GetMute(self): return self.muted
    def SetMute(self, m, ctx): self.muted = m


def test_volume_actions(monkeypatch):
    ep = FakeEndpoint(0.5)
    monkeypatch.setattr(system_control, "_endpoint", lambda: ep)
    assert system_control.volume("up") == "Volume set to 60%."
    assert system_control.volume("down", step=25) == "Volume set to 35%."
    assert system_control.volume("set", level=150) == "Volume set to 100%."
    assert system_control.volume("mute") == "Volume muted." and ep.muted == 1
    assert "muted" in system_control.volume("get")
    system_control.volume("set", level=20)
    assert ep.muted == 0                       # setting a level unmutes
    assert system_control.volume("dance").startswith("Error:")
    assert system_control.volume("set").startswith("Error:")


def test_volume_device_error(monkeypatch):
    def boom():
        raise OSError("no device")
    monkeypatch.setattr(system_control, "_endpoint", boom)
    assert system_control.volume("up").startswith("Error:")


# ── brightness ──────────────────────────────────────────────────────────────

def test_brightness_actions(monkeypatch):
    state = {"v": 50}
    fake = SimpleNamespace(get_brightness=lambda display=0: [state["v"]],
                           set_brightness=lambda v, display=0: state.update(v=v))
    monkeypatch.setattr(system_control, "_sbc", lambda: fake)
    assert system_control.brightness("up") == "Brightness set to 60%."
    assert system_control.brightness("set", level=0) == "Brightness set to 5%."   # never fully dark
    assert system_control.brightness("get") == "Brightness is 5%."


def test_brightness_unsupported(monkeypatch):
    def nope(display=0):
        raise RuntimeError("no ddc")
    monkeypatch.setattr(system_control, "_sbc", lambda: SimpleNamespace(get_brightness=nope))
    assert "not supported" in system_control.brightness("up")


# ── lock ────────────────────────────────────────────────────────────────────

def test_lock_pc(monkeypatch):
    calls = []
    monkeypatch.setattr(system_control, "_lock_workstation", lambda: calls.append(1) or True)
    assert system_control.lock_pc() == "PC locked." and calls == [1]
    monkeypatch.setattr(system_control, "_lock_workstation", lambda: False)
    assert system_control.lock_pc().startswith("Error:")


# ── screenshot ──────────────────────────────────────────────────────────────

def _decode_png(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunks.setdefault(tag, b"")
        chunks[tag] += data[pos + 8:pos + 8 + length]
        pos += 12 + length
    w, h = struct.unpack(">II", chunks[b"IHDR"][:8])
    return w, h, zlib.decompress(chunks[b"IDAT"])


def test_encode_png_converts_bgra_to_rgb():
    bgra = bytes([255, 0, 0, 255,  0, 255, 0, 255,  0, 0, 255, 255,  10, 20, 30, 255])  # 2x2
    w, h, raw = _decode_png(system_control.encode_png(2, 2, bgra))
    assert (w, h) == (2, 2)
    assert raw == bytes([0, 0, 0, 255, 0, 255, 0,  0, 255, 0, 0, 30, 20, 10])


def test_screenshot_saved_inside_workspace(workspace, monkeypatch):
    monkeypatch.setattr(system_control, "_grab_screen", lambda: (2, 1, bytes(8)))
    result = system_control.take_screenshot(name="demo shot!")
    assert result.startswith("Screenshot saved to workspace/screenshots/demoshot_")
    files = list((workspace / "screenshots").glob("*.png"))
    assert len(files) == 1 and files[0].read_bytes()[:4] == b"\x89PNG"


def test_screenshot_dir_outside_workspace_rejected(workspace, monkeypatch):
    monkeypatch.setattr(system_control, "_tools_cfg", lambda: {"screenshots_dir": "../../escape"})
    monkeypatch.setattr(system_control, "_grab_screen", lambda: pytest.fail("must not capture"))
    assert system_control.take_screenshot().startswith("Error:")


# ── system info ─────────────────────────────────────────────────────────────

def test_system_info(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 12.0)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(percent=40.0, used=4 * 2**30, total=16 * 2**30))
    monkeypatch.setattr(psutil, "sensors_battery", lambda: SimpleNamespace(percent=80.0, power_plugged=False, secsleft=5400))
    out = system_control.system_info()
    assert "CPU 12%" in out and "RAM 40% used (4.0 of 16.0 GB)" in out
    assert "battery 80% (on battery, about 1 h 30 min left)" in out
    monkeypatch.setattr(psutil, "sensors_battery", lambda: None)
    assert "no battery" in system_control.system_info()


# ── reminders ───────────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def test_parse_when_formats():
    local_now = NOW.astimezone()
    due = reminders.parse_when("23:59", now=NOW).astimezone()
    assert due > local_now and (due.hour, due.minute) == (23, 59)
    past = (local_now - timedelta(minutes=5)).strftime("%H:%M")
    assert reminders.parse_when(past, now=NOW) > NOW        # rolls to tomorrow
    assert reminders.parse_when("2026-12-31 08:30").astimezone().strftime("%Y-%m-%d %H:%M") == "2026-12-31 08:30"
    for bad in ("25:00", "noon", "", "12:60"):
        with pytest.raises(ValueError):
            reminders.parse_when(bad)


def test_reminders_survive_restart(tmp_path):
    path = tmp_path / "reminders.json"
    s1 = reminders.ReminderScheduler(path=path)
    item = s1.add(NOW + timedelta(hours=1), "stand up")
    s2 = reminders.ReminderScheduler(path=path)              # "restart"
    assert [i["id"] for i in s2.list()] == [item["id"]]
    assert s2.cancel(item["id"]) is True
    assert reminders.ReminderScheduler(path=path).list() == []


def test_corrupt_reminders_file(tmp_path):
    path = tmp_path / "reminders.json"
    path.write_text("{oops")
    assert reminders.ReminderScheduler(path=path).list() == []


def test_pop_due_and_limit(tmp_path):
    clock = [NOW]
    s = reminders.ReminderScheduler(path=tmp_path / "r.json", clock=lambda: clock[0])
    s.add(NOW + timedelta(seconds=30), "a")
    s.add(NOW + timedelta(hours=2), "b")
    assert s.pop_due() == []
    clock[0] = NOW + timedelta(minutes=1)
    assert [i["text"] for i in s.pop_due()] == ["a"]
    assert [i["text"] for i in s.list()] == ["b"]
    assert 0 < s.seconds_until_next() <= 7200


async def test_scheduler_fires_event_and_callback(tmp_path, captured):
    fired = []

    async def on_due(item):
        fired.append(item)

    s = reminders.ReminderScheduler(path=tmp_path / "r.json", on_due=on_due)
    s.start(asyncio.get_running_loop())
    s.add(datetime.now(timezone.utc) + timedelta(milliseconds=100), "tea")
    for _ in range(40):
        if fired:
            break
        await asyncio.sleep(0.05)
    s.stop()
    assert fired and fired[0]["text"] == "tea" and fired[0]["missed"] is False
    assert any(e["type"] == "reminder.due" for e in captured)


async def test_missed_reminder_fires_at_start(tmp_path):
    path = tmp_path / "r.json"
    reminders.ReminderScheduler(path=path).add(datetime.now(timezone.utc) - timedelta(hours=1), "missed one")
    fired = []
    s = reminders.ReminderScheduler(path=path, on_due=lambda i: asyncio.sleep(0, fired.append(i)))
    s.start(asyncio.get_running_loop())
    await asyncio.sleep(0.1)
    s.stop()
    assert fired and fired[0]["missed"] is True and "missed" in fired[0]["spoken"]


def test_timer_and_reminder_tools(tmp_path):
    reminders.register_scheduler(reminders.ReminderScheduler(path=tmp_path / "r.json"))
    assert reminders_tool.set_timer(minutes=5, label="pasta").startswith("Timer set for 5 min")
    assert reminders_tool.set_timer(seconds=0).startswith("Error:")
    assert reminders_tool.set_timer(minutes=60 * 25).startswith("Error:")
    assert reminders_tool.set_reminder("2020-01-01 10:00", "old").startswith("Error:")
    assert reminders_tool.set_reminder("nonsense", "x").startswith("Error:")
    assert "pasta" in reminders_tool.list_reminders()
    rid = reminders.get_scheduler().list()[0]["id"]
    assert reminders_tool.cancel_reminder(rid) == f"Cancelled {rid}."
    assert reminders_tool.cancel_reminder("zzz").startswith("Error:")


# ── countdown ───────────────────────────────────────────────────────────────

async def test_countdown_completes_with_events(captured):
    done = await countdown.run_countdown("Sending to Sam", seconds=1, slice_s=0.02)
    assert done is True
    cd = [e for e in captured if e["type"] == "policy.countdown"]
    assert cd and cd[0]["data"]["seconds_left"] == 1


async def test_countdown_cancel_request():
    asyncio.get_running_loop().call_later(0.05, countdown.request_cancel)
    assert await countdown.run_countdown("x", seconds=2, slice_s=0.01) is False


async def test_countdown_cancelled_by_kill_switch():
    asyncio.get_running_loop().call_later(0.05, KILL_SWITCH.stop)
    assert await countdown.run_countdown("x", seconds=2, slice_s=0.01) is False


async def test_countdown_speaks_announcement():
    spoken = []

    async def speak(text):
        spoken.append(text)
    await countdown.run_countdown("Sending", seconds=1, speak=speak, slice_s=0.02)
    assert spoken and "Say stop to cancel" in spoken[0]


# ── agent fixes ─────────────────────────────────────────────────────────────

class _Call:
    def __init__(self, cid, name, args):
        self.id = cid
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = json.dumps(args)


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


def _provider(*responses):
    pm = MagicMock(spec=ProviderManager)
    pm.ask = AsyncMock(side_effect=list(responses))
    return pm


async def test_async_confirm_after_retries_is_awaited(monkeypatch, captured):
    """Regression: an async confirm callback used to be called without await
    (a coroutine is truthy), so a failing tool retried forever."""
    prompts = []

    async def confirm(msg):
        prompts.append(msg)
        return False

    tool = MagicMock(return_value="Error: broken")
    monkeypatch.setitem(TOOL_FUNCTIONS, "flaky", tool)
    monkeypatch.setitem(TOOL_REGISTRY, "flaky", {"risk": RiskLevel.LOW, "allowlisted": True,
                                                 "affects_people": False, "outside_workspace": False,
                                                 "forbidden_category": None})
    runner = AgentRunner(_provider((_Msg(tool_calls=[_Call("1", "flaky", {})]), "groq"), (_Msg("done"), "groq")),
                         ModeManager(), confirm_callback=confirm, max_retries=2)
    reply, _ = await asyncio.wait_for(runner.run([{"role": "user", "content": "go"}]), timeout=5)
    assert reply == "done" and tool.call_count == 3 and len(prompts) == 1
    assert any(e["type"] == "tool.error" for e in captured)


async def test_free_mode_countdown_runs_people_tool(monkeypatch, captured):
    tool = MagicMock(return_value="sent")
    monkeypatch.setitem(TOOL_FUNCTIONS, "send_message", tool)
    mm = ModeManager(pin=TEST_PIN)
    mm.enter_free(pin=TEST_PIN)
    runner = AgentRunner(
        _provider((_Msg(tool_calls=[_Call("1", "send_message", {"recipient": "Sam", "text": "secret words"})]), "groq"),
                  (_Msg("ok"), "groq")),
        mm, confirm_callback=lambda _: pytest.fail("Free mode must not ask"), countdown_seconds=1)
    await runner.run([{"role": "user", "content": "send"}])
    assert tool.call_count == 1
    cd = [e for e in captured if e["type"] == "policy.countdown"]
    assert cd and "Sam" in cd[0]["text"]
    assert all("secret words" not in e["text"] for e in cd)   # countdown never shows message text
    mm.enter_restricted()


async def test_free_mode_countdown_cancelled(monkeypatch):
    tool = MagicMock(return_value="sent")
    monkeypatch.setitem(TOOL_FUNCTIONS, "send_message", tool)
    mm = ModeManager(pin=TEST_PIN)
    mm.enter_free(pin=TEST_PIN)
    runner = AgentRunner(
        _provider((_Msg(tool_calls=[_Call("1", "send_message", {"recipient": "Sam"})]), "groq"), (_Msg("ok"), "groq")),
        mm, confirm_callback=lambda _: False, countdown_seconds=2)
    asyncio.get_running_loop().call_later(0.2, countdown.request_cancel)
    await runner.run([{"role": "user", "content": "send"}])
    assert tool.call_count == 0
    mm.enter_restricted()


# ── M2 Security & Isolation Tests ──────────────────────────────────────────

def test_take_screenshot_policy_risk_and_directory(workspace, monkeypatch):
    """take_screenshot must ask in Restricted, allow in Free, and stay inside workspace/."""
    assert check("take_screenshot", Mode.RESTRICTED) == "ask"
    assert check("take_screenshot", Mode.FREE) == "allow"

    monkeypatch.setattr(system_control, "_grab_screen", lambda: (2, 2, bytes(16)))
    res = system_control.take_screenshot(name="safe_test")
    assert "workspace/screenshots/safe_test_" in res
    created = list((workspace / "screenshots").glob("*.png"))
    assert len(created) == 1
    # Check that file path is strictly within workspace
    assert created[0].resolve().is_relative_to(workspace)


def test_system_info_returns_no_secrets(monkeypatch):
    """system_info must never return or leak environment keys or the PIN."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_super_secret_test_key_12345")
    monkeypatch.setenv("KIRA_FREE_MODE_PIN", "4321")
    info = system_control.system_info()
    assert "gsk_super_secret" not in info
    assert "4321" not in info
    assert "CPU" in info and "RAM" in info


@pytest.mark.asyncio
async def test_user_text_cannot_answer_ask_first_confirmation():
    """user_text from avatar chat box must NEVER act as an answer to ask-first confirmation."""
    from backend.voice import async_shared_confirm

    user_queue = asyncio.Queue()
    # Queue a chat_box message with "yes"
    await user_queue.put(("chat_box", "yes", None))

    approved = await async_shared_confirm(
        "Delete database?",
        timeout_seconds=0.15,
        user_queue=user_queue,
    )
    # Must NOT have approved via the chat_box user_text
    assert approved is False
    # The chat_box message must be preserved in the queue for regular chat processing
    assert not user_queue.empty()
    item = await user_queue.get()
    assert item[0] == "chat_box" and item[1] == "yes"
