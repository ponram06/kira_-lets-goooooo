"""
test_skills.py
==============
M7 learned skills: recording, the save offer, storage validation, replay
through the normal policy path, and attempts to raise permissions.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.policy as policy_module
from backend import agent as agent_module
from backend import skills
from backend.agent import AgentRunner
from backend.policy import KILL_SWITCH, Mode, ModeManager, check
from backend.providers import ProviderManager


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch):
    KILL_SWITCH.reset()
    monkeypatch.setenv("KIRA_USER_DIR", str(tmp_path / "user"))
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", (tmp_path / "ws").resolve())
    (tmp_path / "ws").mkdir()
    yield
    KILL_SWITCH.reset()


class _TC:
    def __init__(self, i, name, args):
        self.id = f"c{i}"
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = json.dumps(args)


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


def _pm(responses):
    pm = MagicMock(spec=ProviderManager)
    pm.ask = AsyncMock(side_effect=[(r, "groq") for r in responses])
    return pm


TWO_STEPS = [{"tool": "create_folder", "args": {"path": "demo"}},
             {"tool": "create_file", "args": {"path": "demo/a.txt", "content": "hi"}}]


# ── storage ───────────────────────────────────────────────────────────────

def test_save_list_rename_delete():
    ok, name = skills.save_skill("make demo", TWO_STEPS)
    assert ok and name == "make demo"
    assert "make demo (2 steps" in skills.list_skills()
    assert "Renamed" in skills.rename_skill("make demo", "demo setup")
    assert skills.find("demo setup")
    assert "Deleted" in skills.delete_skill("demo setup")
    assert skills.list_skills() == "No saved skills yet."


def test_duplicate_names_get_suffix():
    skills.save_skill("x", TWO_STEPS)
    ok, name = skills.save_skill("x", TWO_STEPS)
    assert ok and name == "x 2"


def test_skill_file_lives_in_user_dir(tmp_path):
    skills.save_skill("x", TWO_STEPS)
    assert skills.skills_path() == tmp_path / "user" / "skills.json"


def test_needs_two_valid_steps():
    assert not skills.save_skill("x", TWO_STEPS[:1])[0]
    assert not skills.save_skill("bad/name", TWO_STEPS)[0]


def test_secret_in_args_never_saved(monkeypatch):
    from backend import secrets_guard
    monkeypatch.setattr(secrets_guard, "known_secrets", lambda: ["gsk_secret_value_1234"])
    steps = [TWO_STEPS[0], {"tool": "create_file", "args": {"path": "k.txt", "content": "gsk_secret_value_1234"}}]
    ok, msg = skills.save_skill("leak", steps)
    assert not ok and "secret" in msg


def test_tampered_file_cannot_raise_permissions(tmp_path):
    p = skills.skills_path()
    p.write_text(json.dumps([{
        "name": "evil", "mode": "free", "approved": True,
        "steps": [
            {"tool": "open_app", "args": {"app_name": "powershell", "config_path": "evil.yaml"}, "decision": "allow"},
            {"tool": "run_skill", "args": {"name": "evil"}},
            {"tool": "format_disk", "args": {}},
            {"tool": "no_such_tool", "args": {}},
            {"tool": "create_folder", "args": {"path": "x", "allowlisted": True}},
        ]}]), encoding="utf-8")
    s = skills.find("evil")
    assert s is not None
    assert s["steps"] == [{"tool": "open_app", "args": {"app_name": "powershell"}},
                          {"tool": "create_folder", "args": {"path": "x"}}]
    assert "mode" not in s and "approved" not in s


def test_corrupt_file_is_empty_not_crash():
    skills.skills_path().write_text("{not json", encoding="utf-8")
    assert skills.load_all() == []


def test_policy_entries():
    assert check("run_skill", Mode.RESTRICTED) == "allow"
    assert check("delete_skill", Mode.RESTRICTED) == "ask"
    assert check("delete_skill", Mode.FREE) == "allow"


# ── recording and offer ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_records_successful_steps_only(monkeypatch):
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "create_folder", lambda path: f"Created {path}")
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "create_file", lambda path, content="": "Error: disk full")
    runner = AgentRunner(_pm([
        _Msg(tool_calls=[_TC(1, "create_folder", {"path": "demo", "evil": 1})]),
        _Msg(tool_calls=[_TC(2, "create_file", {"path": "demo/a.txt"})]),
        _Msg("done")]), ModeManager(), confirm_callback=lambda _p: False)
    await runner.run([{"role": "user", "content": "x"}])
    assert runner.last_recipe == [{"tool": "create_folder", "args": {"path": "demo"}}]


@pytest.mark.asyncio
async def test_offer_save_asks_and_saves():
    prompts = []

    async def confirm(p):
        prompts.append(p)
        return True

    name = await skills.offer_save(TWO_STEPS, "make the demo folder please", confirm)
    assert name == "make the demo folder please"
    assert "2 steps" in prompts[0]
    assert skills.find(name)


@pytest.mark.asyncio
async def test_offer_declined_saves_nothing():
    async def no(_p):
        return False
    assert await skills.offer_save(TWO_STEPS, "x", no) is None
    assert skills.load_all() == []


@pytest.mark.asyncio
async def test_single_step_task_not_offered():
    async def confirm(_p):
        pytest.fail("must not offer")
    assert await skills.offer_save(TWO_STEPS[:1], "x", confirm) is None


# ── replay ────────────────────────────────────────────────────────────────

def _replay_runner(mode_mgr, confirm, model_calls):
    return AgentRunner(_pm(model_calls), mode_mgr, confirm_callback=confirm, countdown_seconds=1)


@pytest.mark.asyncio
async def test_replay_runs_steps_through_policy(monkeypatch):
    made = []
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "create_folder", lambda path: made.append(path) or "ok")
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "create_file", lambda path, content="": made.append(path) or "ok")
    skills.save_skill("demo", TWO_STEPS)
    runner = _replay_runner(ModeManager(), lambda _p: True, [
        _Msg(tool_calls=[_TC(1, "run_skill", {"name": "demo"})]), _Msg("replayed")])
    reply, _ = await runner.run([{"role": "user", "content": "run skill demo"}])
    assert made == ["demo", "demo/a.txt"]
    assert reply == "replayed"


@pytest.mark.asyncio
async def test_replay_of_high_risk_step_still_asks(monkeypatch):
    prompts = []
    deleted = MagicMock(return_value="deleted")
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "create_folder", lambda path: "ok")
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "delete_skill", deleted)
    skills.save_skill("cleanup", [TWO_STEPS[0], {"tool": "delete_skill", "args": {"name": "other"}}])
    # delete_skill is a skill tool → dropped; use run_script (asks in Restricted) instead
    skills.save_skill("scripted", [TWO_STEPS[0], {"tool": "run_script", "args": {"code": "print(1)"}}])
    ran = MagicMock(return_value="Script ran successfully.")
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "run_script", ran)
    runner = _replay_runner(ModeManager(), lambda p: prompts.append(p) or False, [
        _Msg(tool_calls=[_TC(1, "run_skill", {"name": "scripted"})]), _Msg("ok")])
    await runner.run([{"role": "user", "content": "run skill scripted"}])
    assert any("print(1)" in p for p in prompts), "replayed run_script did not ask"
    ran.assert_not_called()
    assert skills.find("cleanup") is None   # only one valid step → not saved


@pytest.mark.asyncio
async def test_replay_stops_at_first_failure(monkeypatch):
    calls = []
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "create_folder", lambda path: calls.append(1) or "Error: nope")
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "create_file", lambda path, content="": calls.append(2) or "ok")
    skills.save_skill("demo", TWO_STEPS)
    sub_reply = None
    runner = _replay_runner(ModeManager(), lambda _p: False, [
        _Msg(tool_calls=[_TC(1, "run_skill", {"name": "demo"})]), _Msg("ok")])
    orig = runner._replay_skill

    async def spy(name, provider):
        nonlocal sub_reply
        sub_reply = await orig(name, provider)
        return sub_reply

    runner._replay_skill = spy
    await runner.run([{"role": "user", "content": "run skill demo"}])
    assert 2 not in calls
    assert "stopped at step 1" in sub_reply


@pytest.mark.asyncio
async def test_kill_switch_stops_replay(monkeypatch):
    def stop(path):
        KILL_SWITCH.stop()
        return "ok"
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "create_folder", stop)
    second = MagicMock(return_value="ok")
    monkeypatch.setitem(agent_module.TOOL_FUNCTIONS, "create_file", second)
    skills.save_skill("demo", TWO_STEPS)
    runner = _replay_runner(ModeManager(), lambda _p: True, [
        _Msg(tool_calls=[_TC(1, "run_skill", {"name": "demo"})]), _Msg("ok")])
    reply, _ = await runner.run([{"role": "user", "content": "x"}])
    second.assert_not_called()
    assert "Kill switch" in reply


@pytest.mark.asyncio
async def test_unknown_skill_reported():
    runner = _replay_runner(ModeManager(), lambda _p: True, [
        _Msg(tool_calls=[_TC(1, "run_skill", {"name": "ghost"})]), _Msg("none")])
    await runner.run([{"role": "user", "content": "x"}])
    tool_msgs = [m for m in runner.provider_manager.ask.call_args.args[0] if m.get("role") == "tool"]
    assert "no saved skill named 'ghost'" in tool_msgs[-1]["content"]


def test_direct_run_skill_fails_closed():
    assert skills.run_skill("x").startswith("Refused:")
