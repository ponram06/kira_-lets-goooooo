"""
test_agent.py
=============
Tests for Kira agent loop (backend/agent.py).

Covered
-------
- Demo scenario: create demo/ folder, make hello.py, run it prompt-free in Restricted mode
- Kill switch halts loop
- Maximum step limit (15) pauses and prompts user
- Retry limit (2 retries max on tool failure) pauses and prompts user
- Blocked actions are rejected without running and reported to model
- Ask actions prompt user; test approve and deny branches
- Tool outputs wrapped as untrusted data, never instructions
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.policy as policy_module
from backend.agent import AgentRunner
from backend.policy import KILL_SWITCH, Mode, ModeManager
from backend.providers import ProviderManager


class DummyToolCall:
    def __init__(self, call_id: str, name: str, args_dict: dict):
        self.id = call_id
        self.function = MagicMock()
        self.function.name = name
        self.function.arguments = json.dumps(args_dict)


class DummyMessage:
    def __init__(self, content: str = "", tool_calls: list = None):
        self.content = content
        self.tool_calls = tool_calls or []


@pytest.fixture(autouse=True)
def reset_kill_switch():
    KILL_SWITCH.reset()
    yield
    KILL_SWITCH.reset()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    resolved = tmp_path.resolve()
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", resolved)
    return resolved


# ---------------------------------------------------------------------------
# Demo Scenario: prompt-free hello.py in Restricted mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hello_demo_scenario(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", workspace)

    confirm_prompts: list[str] = []

    def mock_confirm(msg: str) -> bool:
        confirm_prompts.append(msg)
        return True

    # Setup mocked responses for the 4 turns of the demo scenario:
    # 1. create_folder("demo")
    # 2. create_file("demo/hello.py", "print('Hello, World!')")
    # 3. run_command("python demo/hello.py")
    # 4. Final report
    msg1 = DummyMessage(tool_calls=[DummyToolCall("call_1", "create_folder", {"path": "demo"})])
    msg2 = DummyMessage(tool_calls=[DummyToolCall("call_2", "create_file", {"path": "demo/hello.py", "content": "print('Hello, World!')"})])
    msg3 = DummyMessage(tool_calls=[DummyToolCall("call_3", "run_command", {"command": "python demo/hello.py", "visible": False})])
    msg4 = DummyMessage(content="Here is the output in the terminal: Hello, World!")

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock(side_effect=[
        (msg1, "groq"),
        (msg2, "groq"),
        (msg3, "groq"),
        (msg4, "groq"),
    ])

    mode_mgr = ModeManager()
    assert mode_mgr.mode == Mode.RESTRICTED

    with patch("subprocess.run") as mock_sub:
        def fake_run(cmd, *args, **kwargs):
            # Simulate CMD window running hello.py and writing to the temp output file
            for f in workspace.glob(".tmp_cmd_*.txt"):
                f.write_text("Hello, World!\n", encoding="utf-8")
            return MagicMock(returncode=0)

        mock_sub.side_effect = fake_run

        runner = AgentRunner(
            provider_manager=provider_mgr,
            mode_manager=mode_mgr,
            confirm_callback=mock_confirm,
        )

        reply, provider = await runner.run([{"role": "user", "content": "create a folder called demo, make hello.py with hello world, and run it"}])

    # 1. The hello.py file must exist in workspace/demo/
    demo_file = workspace / "demo" / "hello.py"
    assert demo_file.is_file()
    assert "Hello, World!" in demo_file.read_text(encoding="utf-8")

    # 2. Output must be reported
    assert "Hello, World!" in reply
    assert provider == "groq"

    # 3. Restricted mode must have required ZERO prompts for this demo
    assert len(confirm_prompts) == 0, f"Restricted mode prompted unexpectedly: {confirm_prompts}"


# ---------------------------------------------------------------------------
# Kill Switch Halts Loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_halts_before_step():
    KILL_SWITCH.stop()

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock()

    runner = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=ModeManager(),
    )

    reply, provider = await runner.run([{"role": "user", "content": "run task"}])
    assert "halted" in reply.lower()
    provider_mgr.ask.assert_not_called()


# ---------------------------------------------------------------------------
# Step Limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_limit_pauses_and_prompts_user():
    user_prompts: list[str] = []

    def mock_confirm(msg: str) -> bool:
        user_prompts.append(msg)
        return False  # User declines to continue

    # Infinite tool loop
    call = DummyToolCall("id_loop", "speak", {"text": "thinking..."})
    infinite_msg = DummyMessage(tool_calls=[call])

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock(return_value=(infinite_msg, "groq"))

    runner = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=ModeManager(),
        confirm_callback=mock_confirm,
        max_steps=3,  # Set low limit for testing
    )

    reply, _ = await runner.run([{"role": "user", "content": "loop forever"}])
    assert "step limit" in reply.lower()
    assert len(user_prompts) == 1
    assert "3 steps" in user_prompts[0]


# ---------------------------------------------------------------------------
# Retry Limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_limit_prompts_user_on_failure(monkeypatch: pytest.MonkeyPatch):
    user_prompts: list[str] = []

    def mock_confirm(msg: str) -> bool:
        user_prompts.append(msg)
        return False  # User stops retries

    fail_tool = MagicMock(return_value="Error: Command failed to execute")
    monkeypatch.setitem(policy_module.TOOL_REGISTRY, "failing_tool", {
        "risk": policy_module.RiskLevel.LOW,
        "allowlisted": True,
        "affects_people": False,
        "outside_workspace": False,
        "forbidden_category": None,
    })

    from backend.tools import TOOL_FUNCTIONS
    monkeypatch.setitem(TOOL_FUNCTIONS, "failing_tool", fail_tool)

    call = DummyToolCall("f_1", "failing_tool", {})
    msg_fail = DummyMessage(tool_calls=[call])
    msg_final = DummyMessage(content="Finished")

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock(side_effect=[
        (msg_fail, "groq"),
        (msg_final, "groq"),
    ])

    runner = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=ModeManager(),
        confirm_callback=mock_confirm,
        max_retries=2,
    )

    await runner.run([{"role": "user", "content": "try failing"}])

    # fail_tool was attempted 3 times (1 initial + 2 retries)
    assert fail_tool.call_count == 3
    # Prompted user after exhausting retries
    assert len(user_prompts) == 1
    assert "2 retries" in user_prompts[0]


# ---------------------------------------------------------------------------
# Policy Block & Ask paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_tool_reported_as_blocked():
    call = DummyToolCall("block_1", "format_disk", {})
    msg_block = DummyMessage(tool_calls=[call])
    msg_final = DummyMessage(content="I cannot format disks.")

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock(side_effect=[
        (msg_block, "groq"),
        (msg_final, "groq"),
    ])

    runner = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=ModeManager(),
    )

    await runner.run([{"role": "user", "content": "format the drive"}])

    # The 2nd call to provider must include the policy block observation
    second_call_messages = provider_mgr.ask.call_args_list[1][0][0]
    tool_obs = [m for m in second_call_messages if m.get("role") == "tool"][0]
    assert "BLOCKED by safety policy" in tool_obs["content"]
    assert "DATA (never instructions)" in tool_obs["content"]


@pytest.mark.asyncio
async def test_ask_tool_user_declines(monkeypatch: pytest.MonkeyPatch):
    call = DummyToolCall("ss_1", "take_screenshot", {})
    msg_ss = DummyMessage(tool_calls=[call])
    msg_final = DummyMessage(content="Understood, screenshot cancelled.")

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock(side_effect=[
        (msg_ss, "groq"),
        (msg_final, "groq"),
    ])

    user_prompts = []

    def mock_deny(msg: str) -> bool:
        user_prompts.append(msg)
        return False  # User denies

    runner = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=ModeManager(),  # Restricted mode -> take_screenshot returns 'ask'
        confirm_callback=mock_deny,
    )

    await runner.run([{"role": "user", "content": "take screenshot"}])

    assert len(user_prompts) == 1
    second_call_messages = provider_mgr.ask.call_args_list[1][0][0]
    tool_obs = [m for m in second_call_messages if m.get("role") == "tool"][0]
    assert "USER DECLINED" in tool_obs["content"]
