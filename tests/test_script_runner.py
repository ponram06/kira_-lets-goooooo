"""
test_script_runner.py
=====================
M5 run_script: static checks, runtime limits, policy, agent flow, sandbox options.
Scripts run in a real local subprocess inside a temp workspace (offline).
NOTE: static checks are not a full sandbox; these tests prove the layers we
have, not that Python cannot be escaped.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.policy as policy_module
from backend import agent as agent_module
from backend.agent import AgentRunner
from backend.policy import KILL_SWITCH, Mode, ModeManager, check
from backend.providers import ProviderManager
from backend.tools import script_runner as sr


@pytest.fixture(autouse=True)
def _ws(tmp_path: Path, monkeypatch):
    KILL_SWITCH.reset()
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", tmp_path.resolve())
    yield tmp_path.resolve()
    KILL_SWITCH.reset()


def _cfg(tmp_path: Path, **over) -> Path:
    data = {"scripts": {"timeout_seconds": 5, "max_output_chars": 2000, "memory_limit_mb": 128,
                        "sandbox": "none", **over}}
    p = tmp_path / "cfg.yaml"
    p.write_text(json.dumps(data), encoding="utf-8")   # JSON is valid YAML
    return p


# ── static checks ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", [
    "import os\nos.system('dir')",
    "import subprocess",
    "from subprocess import run",
    "import socket",
    "import ctypes",
    "import shutil\nshutil.rmtree('x')",
    "import importlib",
    "import sys",
    "import pathlib",
    "eval('1+1')",
    "exec('print(1)')",
    "compile('1','x','eval')",
    "__import__('os')",
    "getattr(print, 'x')",
    "().__class__.__bases__[0].__subclasses__()",
    "open('C:/Windows/win.ini')",
    "open('../../secret.txt')",
    "open('\\\\\\\\server\\\\share\\\\x')",
    "p = 'x.txt'\nopen(p)",
    "import requests",
    "from . import x",
    "import numpy",          # not on the allow-list
    "def f(:\n  pass",       # syntax error
    "",
])
def test_static_checks_reject(code):
    with pytest.raises(sr.ScriptRejected):
        sr.check_script(code)


@pytest.mark.parametrize("code", [
    "print(sum(range(10)))",
    "import math\nprint(math.sqrt(16))",
    "from collections import Counter\nprint(Counter('aab'))",
    "with open('notes.txt', 'w') as f:\n    f.write('hi')",
    "if __name__ == '__main__':\n    print('ok')",
])
def test_static_checks_accept(code):
    sr.check_script(code)


def test_code_size_limit():
    with pytest.raises(sr.ScriptRejected):
        sr.check_script("x = 1\n" * 5000, max_chars=1000)


# ── runtime ───────────────────────────────────────────────────────────────

def test_runs_and_captures_output(_ws):
    out = sr.run_script("print('Hello, World!')", name="hello", config_path=_cfg(_ws))
    assert "ran successfully" in out and "Hello, World!" in out
    saved = list((_ws / "scripts").glob("hello_*.py"))
    assert len(saved) == 1


def test_working_directory_is_scripts_dir(_ws):
    out = sr.run_script("with open('made.txt','w') as f:\n    f.write('x')\nprint('done')",
                        config_path=_cfg(_ws))
    assert "done" in out
    assert (_ws / "scripts" / "made.txt").is_file()
    assert not (_ws / "made.txt").exists()


def test_rejected_script_is_not_saved(_ws):
    out = sr.run_script("import os", config_path=_cfg(_ws))
    assert out.startswith("Refused:")
    assert not list((_ws / "scripts").glob("*.py")) if (_ws / "scripts").exists() else True


def test_infinite_loop_times_out(_ws):
    out = sr.run_script("while True:\n    pass", config_path=_cfg(_ws, timeout_seconds=1))
    assert "timeout" in out


def test_huge_output_is_capped(_ws):
    out = sr.run_script("while True:\n    print('x' * 1000)", config_path=_cfg(_ws, max_output_chars=3000))
    assert "exceeded 3000" in out
    assert len(out) < 3500


@pytest.mark.skipif(os.name != "nt", reason="Job Object memory cap is Windows-only")
def test_huge_memory_is_stopped(_ws):
    out = sr.run_script("x = bytearray(600 * 1024 * 1024)\nprint('allocated')",
                        config_path=_cfg(_ws, memory_limit_mb=64))
    assert "allocated" not in out.split("Output:")[-1]
    assert out.startswith("Error:")


def test_child_env_has_no_secrets(_ws, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_testsecret_value_123")
    monkeypatch.setenv("KIRA_FREE_MODE_PIN", "0000")
    env = sr._clean_env()
    assert "GROQ_API_KEY" not in env and "KIRA_FREE_MODE_PIN" not in env
    out = sr.run_script("import json\nprint('ok')", config_path=_cfg(_ws))
    assert "gsk_testsecret" not in out


def test_kill_switch_stops_running_script(_ws):
    timer = threading.Timer(0.5, KILL_SWITCH.stop)
    timer.start()
    out = sr.run_script("while True:\n    pass", config_path=_cfg(_ws, timeout_seconds=10))
    timer.cancel()
    assert "Kill switch" in out


def test_kill_switch_before_start(_ws):
    KILL_SWITCH.stop()
    assert "Kill switch" in sr.run_script("print(1)", config_path=_cfg(_ws))


def test_script_error_reported_honestly(_ws):
    out = sr.run_script("raise ValueError('boom')", config_path=_cfg(_ws))
    assert out.startswith("Error:") and "boom" in out


def test_unsafe_name_is_replaced(_ws):
    sr.run_script("print(1)", name="..\\..\\evil", config_path=_cfg(_ws))
    assert list((_ws / "scripts").glob("script_*.py"))


# ── sandbox options ───────────────────────────────────────────────────────

def test_missing_sandbox_refuses_instead_of_running(_ws, monkeypatch):
    monkeypatch.setattr(sr, "sandbox_available", lambda kind: kind == "none")
    out = sr.run_script("print(1)", config_path=_cfg(_ws, sandbox="docker"))
    assert out.startswith("Refused:") and "not available" in out


def test_unknown_sandbox_value(_ws):
    assert sr.run_script("print(1)", config_path=_cfg(_ws, sandbox="vm")).startswith("Error:")


def test_docker_command_has_no_network_and_memory_cap(tmp_path):
    cmd = sr.build_docker_command(tmp_path / "s.py", {"memory_limit_mb": 128, "docker_image": "python:3.11-slim"})
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
    assert "128m" in cmd and "--read-only" in cmd


def test_wsb_config_disables_networking(tmp_path):
    xml = sr.build_wsb_config(tmp_path, tmp_path / "s.py")
    assert "<Networking>Disable</Networking>" in xml
    assert "<ClipboardRedirection>Disable</ClipboardRedirection>" in xml


# ── policy and agent ──────────────────────────────────────────────────────

def test_policy_restricted_asks_free_counts_down():
    assert check("run_script", Mode.RESTRICTED) == "ask"
    assert check("run_script", Mode.FREE) == "countdown"
    KILL_SWITCH.stop()
    assert check("run_script", Mode.FREE) == "block"


class _TC:
    def __init__(self, name, args):
        self.id = "c1"
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = json.dumps(args)


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


def _runner(responses, confirm, mm=None):
    pm = MagicMock(spec=ProviderManager)
    pm.ask = AsyncMock(side_effect=[(r, "groq") for r in responses])
    return AgentRunner(pm, mm or ModeManager(), confirm_callback=confirm, countdown_seconds=1)


@pytest.mark.asyncio
async def test_agent_restricted_shows_script_and_asks(monkeypatch):
    prompts = []
    ran = MagicMock(return_value="Script ran successfully. Output:\n4")
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "run_script", ran)
    runner = _runner([_Msg(tool_calls=[_TC("run_script", {"code": "print(2+2)"})]), _Msg("4")],
                     confirm=lambda p: prompts.append(p) or False)
    await runner.run([{"role": "user", "content": "compute 2+2 with a script"}])
    assert prompts and "print(2+2)" in prompts[0]
    ran.assert_not_called()


@pytest.mark.asyncio
async def test_agent_rejected_script_never_asks_or_runs(monkeypatch):
    ran = MagicMock()
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "run_script", ran)
    runner = _runner([_Msg(tool_calls=[_TC("run_script", {"code": "import os\nos.system('del *')"})]), _Msg("no")],
                     confirm=lambda p: pytest.fail("must not ask for a rejected script"))
    await runner.run([{"role": "user", "content": "x"}])
    ran.assert_not_called()


@pytest.mark.asyncio
async def test_agent_free_mode_countdown_then_runs(monkeypatch):
    ran = MagicMock(return_value="Script ran successfully. Output:\n4")
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "run_script", ran)
    mm = ModeManager(pin="0000")
    assert mm.enter_free("0000")
    runner = _runner([_Msg(tool_calls=[_TC("run_script", {"code": "print(2+2)"})]), _Msg("4")],
                     confirm=lambda p: pytest.fail("Free mode must not ask"), mm=mm)
    await runner.run([{"role": "user", "content": "x"}])
    mm.enter_restricted()
    ran.assert_called_once()
    assert "config_path" not in ran.call_args.kwargs
