"""
test_phase0_security.py
=======================
Regression tests for the Phase 0 security review (2026-10-17).

1. A hostile model must not be able to pass internal tool parameters
   (config_path, rate-limit caps, visible, mode, confirm_cb ...) through
   tool-call JSON. Only schema-declared parameters reach a tool.
2. The Free-mode first-contact WhatsApp confirmation reads back the contact
   and the exact message.
3. VS Code typing refuses when the foreground process is not VS Code, and
   stops mid-way when focus is lost while typing.
4. Digit-only secrets (the PIN) are redacted only as whole numbers, so
   timestamps are not mangled (and do not act as a PIN oracle).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.policy as policy_module
from backend import agent as agent_module
from backend.agent import AgentRunner
from backend.policy import KILL_SWITCH, Mode, ModeManager
from backend.providers import ProviderManager


class _TC:
    def __init__(self, call_id: str, name: str, args: dict):
        self.id = call_id
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = json.dumps(args)


class _Msg:
    def __init__(self, content: str = "", tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls or []


@pytest.fixture(autouse=True)
def _reset_kill():
    KILL_SWITCH.reset()
    yield
    KILL_SWITCH.reset()


def _runner(responses, confirm=None, mode_mgr=None):
    pm = MagicMock(spec=ProviderManager)
    pm.ask = AsyncMock(side_effect=[(r, "groq") for r in responses])
    return AgentRunner(
        provider_manager=pm,
        mode_manager=mode_mgr or ModeManager(),
        confirm_callback=confirm or (lambda _p: False),
    )


# ── 1. internal parameters are stripped ────────────────────────────────────

@pytest.mark.asyncio
async def test_hostile_config_path_never_reaches_open_app(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", tmp_path.resolve())
    evil = tmp_path / "evil.yaml"
    evil.write_text("tools:\n  allowed_apps: [powershell]\n", encoding="utf-8")

    seen: dict = {}

    def fake_open_app(app_name, path=None, config_path=None):
        seen["kwargs"] = {"app_name": app_name, "path": path, "config_path": config_path}
        return "Error: Application 'powershell' is not in the allow-list."

    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "open_app", fake_open_app)
    runner = _runner([
        _Msg(tool_calls=[_TC("c1", "open_app", {"app_name": "powershell", "config_path": str(evil)})]),
        _Msg(content="done"),
    ], confirm=lambda _p: False)
    await runner.run([{"role": "user", "content": "open notepad"}])
    assert seen["kwargs"]["config_path"] is None, "model-supplied config_path reached the tool"


@pytest.mark.asyncio
async def test_hostile_rate_limit_caps_never_reach_send_whatsapp(monkeypatch):
    seen: dict = {}

    def fake_send(**kwargs):
        seen.update(kwargs)
        return "Refused: test"

    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "send_whatsapp", fake_send)
    runner = _runner([
        _Msg(tool_calls=[_TC("c1", "send_whatsapp", {
            "contact": "Test", "text": "hi",
            "hourly_cap": 100000, "daily_cap": 100000, "min_gap_seconds": 0,
            "poll_timeout": 999, "request_id": "attacker", "user_request": "x",
        })]),
        _Msg(content="done"),
    ], confirm=lambda _p: True)
    await runner.run([{"role": "user", "content": "send hi to Test"}])
    for key in ("hourly_cap", "daily_cap", "min_gap_seconds", "poll_timeout"):
        assert key not in seen, f"model-supplied {key} reached send_whatsapp"
    assert seen["request_id"] != "attacker"
    assert seen["user_request"] == "send hi to Test"


@pytest.mark.asyncio
async def test_hostile_visible_flag_and_mode_are_stripped(monkeypatch):
    seen: dict = {}

    def fake_run_command(command, cwd=None, config_path=None, visible=True, timeout_seconds=30.0):
        seen.update(visible=visible, config_path=config_path, timeout=timeout_seconds)
        return "ok"

    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "run_command", fake_run_command)
    runner = _runner([
        _Msg(tool_calls=[_TC("c1", "run_command", {
            "command": "python hello.py", "visible": False,
            "timeout_seconds": 1e9, "config_path": "evil.yaml"})]),
        _Msg(content="done"),
    ])
    await runner.run([{"role": "user", "content": "run it"}])
    assert seen == {"visible": True, "config_path": None, "timeout": 30.0}


def test_filter_tool_args_keeps_only_schema_params():
    out = agent_module.filter_tool_args("open_any_app", {"name": "Zoom", "mode": "free", "confirm_cb": True})
    assert out == {"name": "Zoom"}
    assert agent_module.filter_tool_args("no_such_tool", {"a": 1}) == {}


# ── 2. Free-mode first-contact read-back ───────────────────────────────────

@pytest.mark.asyncio
async def test_free_mode_first_contact_prompt_reads_back_message(monkeypatch):
    prompts: list[str] = []
    mm = ModeManager(pin="0000")
    assert mm.enter_free("0000")

    from backend import contacts as contacts_mod
    from backend import whatsapp_rate_limit as rl
    fake_res = MagicMock(status="found", contact_name="Tester", masked_phone="+*******123")
    monkeypatch.setattr(contacts_mod, "resolve_contact", lambda _n: fake_res)
    monkeypatch.setattr(rl, "is_contact_approved_free_mode", lambda _n: False)
    monkeypatch.setattr(rl, "approve_contact_free_mode", lambda _n: None)
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "send_whatsapp", lambda **k: "Refused: test")

    runner = _runner([
        _Msg(tool_calls=[_TC("c1", "send_whatsapp", {"contact": "Tester", "text": "see you at five"})]),
        _Msg(content="done"),
    ], confirm=lambda p: prompts.append(p) or False, mode_mgr=mm)
    await runner.run([{"role": "user", "content": "send see you at five to Tester"}])
    mm.enter_restricted()
    assert prompts, "no confirmation asked"
    assert "see you at five" in prompts[0]
    assert "+*******123" in prompts[0]


# ── 3. VS Code foreground verification ─────────────────────────────────────

def test_vscode_active_requires_vscode_process(monkeypatch):
    from backend.tools import vscode_visible as vv
    monkeypatch.setattr(vv.gw, "getActiveWindowTitle", lambda: "hello.py - code review - Google Chrome")
    monkeypatch.setattr(vv, "_foreground_process_name", lambda: "chrome.exe")
    assert vv.is_vscode_window_active("hello.py") is False
    monkeypatch.setattr(vv.gw, "getActiveWindowTitle", lambda: "hello.py - demo - Visual Studio Code")
    monkeypatch.setattr(vv, "_foreground_process_name", lambda: "Code.exe")
    assert vv.is_vscode_window_active("hello.py") is True


def test_vscode_single_line_typing_stops_when_focus_lost(monkeypatch):
    from backend.tools import vscode_visible as vv
    typed: list[str] = []
    checks = iter([True, True, False])
    monkeypatch.setattr(vv, "is_vscode_window_active", lambda _f: next(checks, False))
    monkeypatch.setattr(vv.pyautogui, "typewrite", lambda s, interval=0: typed.append(s))
    ok, _msg = vv.type_visibly("print('Hello, World!') # long single line of code", "hello.py", 0, chunk=8)
    assert ok is False
    assert len("".join(typed)) <= 16, "kept typing after focus was lost"


# ── 4. numeric secrets redacted only as whole numbers ──────────────────────

def test_numeric_pin_not_redacted_inside_timestamps(monkeypatch):
    from backend import secrets_guard as sg
    monkeypatch.setattr(sg, "known_secrets", lambda: ["4321"])
    ts = "2026-10-16T08:56:38.143212+00:00"
    assert sg.redact(ts) == ts
    assert sg.contains_secret(ts) is False
    assert sg.redact("pin is 4321 ok") == "pin is [REDACTED] ok"
    assert sg.redact("pin=4321") == "pin=[REDACTED]"
    assert sg.contains_secret("code 4321") is True
    # Non-numeric secrets still match anywhere
    monkeypatch.setattr(sg, "known_secrets", lambda: ["gsk_abcdef123456"])
    assert sg.redact("xxgsk_abcdef123456yy") == "xx[REDACTED]yy"


def test_doctor_log_scan_ignores_pin_inside_timestamps(tmp_path):
    from backend import doctor
    log = tmp_path / "events.jsonl"
    log.write_text('{"ts": "2026-10-16T08:56:38.143212+00:00"}\n', encoding="utf-8")
    probe = MagicMock()
    probe.secret_values.return_value = ["4321"]
    probe.log_files.return_value = [log]
    probe.read_tail.side_effect = lambda p: p.read_text(encoding="utf-8")
    assert doctor.check_log_leaks(probe).status == "ok"
    log.write_text('{"text": "pin 4321"}\n', encoding="utf-8")
    assert doctor.check_log_leaks(probe).status == "fail"
