"""
test_screen_control.py
======================
M6 screen control with a fake vision model, fake screenshots, fake input.
No real screen, mouse, keyboard or network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend import events as _events
from backend.agent import AgentRunner
from backend.policy import KILL_SWITCH, Mode, ModeManager, check
from backend.providers import ProviderManager
from backend.screen import capture, geometry, safety
from backend.screen.controller import ScreenController, ScreenDeps
from backend.screen.geometry import Rect, ScreenMap
from backend.screen.vision import parse_action, resolve_provider
from backend.tools import screen_tool


@pytest.fixture(autouse=True)
def _reset():
    KILL_SWITCH.reset()
    yield
    KILL_SWITCH.reset()


@pytest.fixture
def captured():
    seen: list[dict] = []
    _events.subscribe(seen.append)
    yield seen
    _events.unsubscribe(seen.append)


# ── geometry / capture ────────────────────────────────────────────────────

def test_downscale_factor_and_mapping_with_negative_monitor():
    virtual = Rect(-1920, 0, 2560, 1440)        # left monitor + primary 2560 wide
    f = geometry.downscale_factor(virtual.width, virtual.height, 1280, 800)
    smap = ScreenMap(virtual, f)
    assert f == 4
    assert smap.to_screen(0, 0) == (-1920 + 2, 2)
    x, y = smap.to_screen(480, 100)              # 480*4 = 1920 → x = 0 + 2 (primary)
    assert x == 2 and y == 402
    assert smap.to_image(x, y) == (480, 100)


def test_point_in_monitor_gap_is_rejected():
    mons = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 3840, 720)]   # right monitor shorter
    smap = ScreenMap(Rect(0, 0, 3840, 1080), 1)
    v, _ = safety.validate_action({"action": "click", "x": 2500, "y": 900}, smap, mons)
    assert not v.ok and "visible monitor" in v.reason
    v, p = safety.validate_action({"action": "click", "x": 2500, "y": 300}, smap, mons)
    assert v.ok and p == (2500, 300)


def test_click_outside_screenshot_rejected():
    smap = ScreenMap(Rect(0, 0, 1920, 1080), 2)
    for x, y in ((-1, 5), (960, 5), (5, 540), (99999, 1)):
        v, _ = safety.validate_action({"action": "click", "x": x, "y": y}, smap, [smap.virtual])
        assert not v.ok


def test_downscale_bgra_keeps_every_nth_pixel():
    w, h = 4, 2
    px = b"".join(bytes([i, 0, 0, 255]) for i in range(w * h))
    sw, sh, small = capture.downscale_bgra(w, h, px, 2)
    assert (sw, sh) == (2, 1)
    assert small == bytes([0, 0, 0, 255, 2, 0, 0, 255])


# ── safety ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,proc", [
    ("Bitwarden", "bitwarden.exe"), ("My Bank - Online Banking", "chrome.exe"),
    ("Windows Security", "SecurityHealthUI.exe"), ("User Account Control", "consent.exe"),
    ("Checkout | Shop", "msedge.exe"), ("Vault - 1Password", "1password.exe"),
    ("", "CredentialUIBroker.exe"),
])
def test_sensitive_windows_refused(title, proc):
    assert not safety.is_sensitive_window(title, proc).ok


def test_normal_window_allowed():
    assert safety.is_sensitive_window("notes.txt - Notepad", "notepad.exe").ok


@pytest.mark.parametrize("text,field", [
    ("hunter2", "Password"), ("123456", ""), ("4111 1111 1111 1111", ""),
    ("abc", "One-time code"), ("x" * 600, ""), ("line1\nline2", ""), ("9999", "PIN"),
])
def test_credential_like_text_never_typed(text, field):
    assert not safety.text_is_safe(text, field).ok


def test_known_secret_never_typed(monkeypatch):
    from backend import secrets_guard
    monkeypatch.setattr(secrets_guard, "known_secrets", lambda: ["gsk_live_secret_abc123"])
    assert not safety.text_is_safe("my key gsk_live_secret_abc123", "").ok


@pytest.mark.parametrize("key,ok", [
    ("enter", True), ("ctrl+s", True), ("tab", True), ("win+r", False), ("ctrl+alt+delete", False),
    ("alt+f4", False), ("win", False), ("ctrl+shift+esc", False),
])
def test_key_allow_list(key, ok):
    assert safety.key_is_allowed(key) is ok


def test_parse_action_tolerates_prose():
    assert parse_action('Sure! {"action": "click", "x": 1, "y": 2}') == {"action": "click", "x": 1, "y": 2}
    assert parse_action("no json") is None
    assert parse_action("{bad json}") is None


# ── controller loop ──────────────────────────────────────────────────────

class FakeShot:
    def __init__(self, digest="a"):
        self.smap = ScreenMap(Rect(0, 0, 1920, 1080), 2)
        self.digest = digest

    def data_url(self):
        return "data:image/png;base64,AAAA"


def make_deps(proposals, mode=Mode.RESTRICTED, confirm=None, fg=("Untitled - Notepad", "notepad.exe"), act=None):
    seq = iter(proposals)
    acted: list = []
    shots = iter(f"d{i}" for i in range(1000))

    async def propose(_msgs):
        p = next(seq, {"action": "done", "reason": "end"})
        if isinstance(p, Exception):
            raise p
        return p

    def default_act(kind, point, action):
        acted.append((kind, point, action.get("text")))

    async def no_sleep(_s):
        return None

    deps = ScreenDeps(
        propose=propose, shoot=lambda: FakeShot(next(shots)), monitors=lambda: [Rect(0, 0, 1920, 1080)],
        foreground=fg if callable(fg) else (lambda: fg), act=act or default_act,
        confirm=confirm or (lambda _p: True), get_mode=lambda: mode, sleep=no_sleep,
    )
    return deps, acted


@pytest.mark.asyncio
async def test_restricted_asks_for_every_action(captured):
    prompts = []
    deps, acted = make_deps(
        [{"action": "click", "x": 10, "y": 10}, {"action": "type", "text": "hello", "x": 10, "y": 10},
         {"action": "key", "key": "ctrl+s"}, {"action": "done", "reason": "saved"}],
        confirm=lambda p: prompts.append(p) or True)
    res = await ScreenController(deps).run("write hello in notepad and save")
    assert res.status == "done"
    assert len(prompts) == 3 and len(acted) == 3
    assert acted[0] == ("click", (21, 21), None)
    # highlight is published before each pointed action
    assert [e for e in captured if e["type"] == "screen.highlight"]


@pytest.mark.asyncio
async def test_declining_stops_without_acting():
    deps, acted = make_deps([{"action": "click", "x": 10, "y": 10}], confirm=lambda _p: False)
    res = await ScreenController(deps).run("x")
    assert res.status == "declined" and acted == []


@pytest.mark.asyncio
async def test_free_mode_does_not_ask():
    deps, acted = make_deps([{"action": "click", "x": 10, "y": 10}, {"action": "done"}], mode=Mode.FREE,
                            confirm=lambda _p: pytest.fail("Free mode must not ask per action"))
    res = await ScreenController(deps).run("x")
    assert res.status == "done" and len(acted) == 1


@pytest.mark.asyncio
async def test_stops_after_ten_actions():
    deps, acted = make_deps([{"action": "scroll", "x": 5, "y": 5, "amount": -1}] * 50, mode=Mode.FREE)
    res = await ScreenController(deps, max_actions=10).run("scroll forever")
    assert res.status == "limit" and len(acted) == 10


@pytest.mark.asyncio
async def test_kill_switch_mid_run_stops_instantly():
    def act(kind, point, action):
        KILL_SWITCH.stop()
    deps, _ = make_deps([{"action": "click", "x": 1, "y": 1}] * 5, mode=Mode.FREE, act=act)
    res = await ScreenController(deps).run("x")
    assert res.status == "halted" and len(res.actions) == 1


@pytest.mark.asyncio
async def test_sensitive_foreground_refuses_before_any_action():
    deps, acted = make_deps([{"action": "click", "x": 1, "y": 1}], fg=("Bitwarden", "bitwarden.exe"))
    res = await ScreenController(deps).run("x")
    assert res.status == "refused" and acted == []


@pytest.mark.asyncio
async def test_window_turning_sensitive_after_confirm_aborts():
    calls = iter([("Notepad", "notepad.exe"), ("User Account Control", "consent.exe")])
    deps, acted = make_deps([{"action": "click", "x": 1, "y": 1}], fg=lambda: next(calls))
    res = await ScreenController(deps).run("x")
    assert res.status == "refused" and acted == []


@pytest.mark.asyncio
async def test_injected_password_typing_is_refused_and_stops():
    # Hostile screen content made the model try to type a password
    deps, acted = make_deps([{"action": "type", "text": "hunter2", "field": "Password", "x": 3, "y": 3}])
    res = await ScreenController(deps).run("log in")
    assert res.status == "refused" and acted == []
    assert "type it yourself" in res.message


@pytest.mark.asyncio
async def test_repeated_invalid_proposals_stop():
    deps, acted = make_deps([{"action": "key", "key": "win+r"}] * 5, mode=Mode.FREE)
    res = await ScreenController(deps).run("x")
    assert res.status == "failed" and acted == []


@pytest.mark.asyncio
async def test_vision_failure_reported_honestly():
    deps, _ = make_deps([RuntimeError("boom")])
    res = await ScreenController(deps).run("x")
    assert res.status == "failed" and "vision model failed" in res.message


@pytest.mark.asyncio
async def test_model_fail_reported():
    deps, _ = make_deps([{"action": "fail", "reason": "button missing"}])
    res = await ScreenController(deps).run("x")
    assert res.status == "failed" and "button missing" in res.message


def test_policy_screen_action_decisions():
    assert check("screen_action", Mode.RESTRICTED) == "ask"
    assert check("screen_action", Mode.FREE) == "allow"
    KILL_SWITCH.stop()
    assert check("screen_action", Mode.FREE) == "block"
    assert check("screen_task", Mode.FREE) == "block"


# ── availability ─────────────────────────────────────────────────────────

def _cfg(tmp_path: Path, provider) -> Path:
    data = {"providers": [{"name": "openrouter", "base_url": "https://x.invalid", "api_key_env": "X", "model": "m"},
                          {"name": "ollama", "base_url": "http://localhost:11434/v1", "api_key_env": None, "model": "q"}],
            "screen_control": {"vision_provider": provider}}
    p = tmp_path / "c.yaml"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_vision_provider_never_ollama():
    assert resolve_provider({"screen_control": {"vision_provider": "ollama"},
                             "providers": [{"name": "ollama", "model": "q"}]}) is None


@pytest.mark.asyncio
async def test_disabled_on_ollama(tmp_path):
    out = await screen_tool.run_screen_task("x", "ollama", ModeManager(), lambda _p: True,
                                            config_path=_cfg(tmp_path, "openrouter"))
    assert out.startswith("Refused:") and "Ollama" in out


@pytest.mark.asyncio
async def test_disabled_without_vision_provider(tmp_path):
    out = await screen_tool.run_screen_task("x", "groq", ModeManager(), lambda _p: True,
                                            config_path=_cfg(tmp_path, None))
    assert out.startswith("Refused:") and "vision provider" in out


@pytest.mark.asyncio
async def test_disabled_after_ollama_forced_restricted(tmp_path):
    mm = ModeManager()
    mm.notify_provider("ollama")
    out = await screen_tool.run_screen_task("x", "groq", mm, lambda _p: True,
                                            config_path=_cfg(tmp_path, "openrouter"))
    assert out.startswith("Refused:")


@pytest.mark.asyncio
async def test_run_screen_task_with_fake_deps(tmp_path):
    deps, acted = make_deps([{"action": "click", "x": 1, "y": 1}, {"action": "done", "reason": "ok"}])
    out = await screen_tool.run_screen_task("x", "groq", ModeManager(), lambda _p: True,
                                            config_path=_cfg(tmp_path, "openrouter"), deps=deps)
    assert out.startswith("Screen control done") and len(acted) == 1


def test_direct_tool_call_fails_closed():
    assert screen_tool.screen_task("anything").startswith("Refused:")


# ── agent integration ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_routes_screen_task_through_controller(monkeypatch):
    called = {}

    async def fake_run(goal, provider, mm, confirm, speak=None):
        called.update(goal=goal, provider=provider)
        return "Screen control done: ok"

    monkeypatch.setattr(screen_tool, "run_screen_task", fake_run)
    tc = MagicMock()
    tc.id = "c1"
    tc.function.name = "screen_task"
    tc.function.arguments = json.dumps({"goal": "click OK", "mode": "free"})
    msg = MagicMock(content="", tool_calls=[tc])
    done = MagicMock(content="finished", tool_calls=[])
    pm = MagicMock(spec=ProviderManager)
    pm.ask = AsyncMock(side_effect=[(msg, "groq"), (done, "groq")])
    reply, _ = await AgentRunner(pm, ModeManager(), confirm_callback=lambda _p: True).run(
        [{"role": "user", "content": "click OK"}])
    assert called == {"goal": "click OK", "provider": "groq"}
    assert reply == "finished"
