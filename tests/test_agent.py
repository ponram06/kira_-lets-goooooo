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


# ---------------------------------------------------------------------------
# Unknown command asks user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_command_asks_user(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", workspace)

    call = DummyToolCall("cmd_1", "run_command", {"command": "git status", "visible": False})
    msg_cmd = DummyMessage(tool_calls=[call])
    msg_final = DummyMessage(content="Here is git status.")

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock(side_effect=[
        (msg_cmd, "groq"),
        (msg_final, "groq"),
    ])

    user_prompts: list[str] = []

    def mock_approve(prompt_msg: str) -> bool:
        user_prompts.append(prompt_msg)
        return True

    with patch("backend.tools.execution.subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.pid = 1234
        mock_popen.return_value = mock_proc

        runner = AgentRunner(
            provider_manager=provider_mgr,
            mode_manager=ModeManager(),
            confirm_callback=mock_approve,
        )

        reply, _ = await runner.run([{"role": "user", "content": "check git"}])

    assert len(user_prompts) == 1
    assert "run_command" in user_prompts[0]
    assert "git status" in user_prompts[0]
    assert reply == "Here is git status."


# ---------------------------------------------------------------------------
# Shell chaining blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chained_command_blocked(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(policy_module, "WORKSPACE_ROOT", workspace)

    call = DummyToolCall("chain_1", "run_command", {"command": "dir & whoami", "visible": False})
    msg_chain = DummyMessage(tool_calls=[call])
    msg_final = DummyMessage(content="Chained command was blocked.")

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock(side_effect=[
        (msg_chain, "groq"),
        (msg_final, "groq"),
    ])

    user_prompts: list[str] = []

    def mock_confirm(msg: str) -> bool:
        user_prompts.append(msg)
        return True

    runner = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=ModeManager(),
        confirm_callback=mock_confirm,
    )

    await runner.run([{"role": "user", "content": "run chained command"}])

    # Chained command must be blocked by policy directly WITHOUT asking user
    assert len(user_prompts) == 0

    second_call_messages = provider_mgr.ask.call_args_list[1][0][0]
    tool_obs = [m for m in second_call_messages if m.get("role") == "tool"][0]
    assert "BLOCKED by safety policy" in tool_obs["content"]
    assert "Shell metacharacters/chaining blocked" in tool_obs["content"]


# ---------------------------------------------------------------------------
# One continue only after step limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_continue_only():
    user_prompts: list[str] = []

    def mock_confirm(msg: str) -> bool:
        user_prompts.append(msg)
        return True  # User approves continuation

    call = DummyToolCall("id_cont", "speak", {"text": "working..."})
    msg_step = DummyMessage(tool_calls=[call])

    provider_mgr = MagicMock(spec=ProviderManager)
    # Return 25 steps of tool calls
    provider_mgr.ask = AsyncMock(return_value=(msg_step, "groq"))

    runner = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=ModeManager(),
        confirm_callback=mock_confirm,
        max_steps=2,
    )

    reply, _ = await runner.run([{"role": "user", "content": "infinite steps"}])

    # First paused at 2 steps, user allowed 15 more (total 17 steps), then halted
    assert "Maximum continuation limit reached" in reply
    assert len(user_prompts) == 1  # Asked only once!


# ---------------------------------------------------------------------------
# Tool output truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_output_truncation(monkeypatch: pytest.MonkeyPatch):
    huge_text = "X" * 6000
    monkeypatch.setitem(policy_module.TOOL_REGISTRY, "huge_tool", {
        "risk": policy_module.RiskLevel.LOW,
        "allowlisted": True,
        "affects_people": False,
        "outside_workspace": False,
        "forbidden_category": None,
    })

    from backend.tools import TOOL_FUNCTIONS
    monkeypatch.setitem(TOOL_FUNCTIONS, "huge_tool", lambda: huge_text)

    call = DummyToolCall("huge_1", "huge_tool", {})
    msg_huge = DummyMessage(tool_calls=[call])
    msg_final = DummyMessage(content="Done reading.")

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock(side_effect=[
        (msg_huge, "groq"),
        (msg_final, "groq"),
    ])

    runner = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=ModeManager(),
    )

    await runner.run([{"role": "user", "content": "get huge text"}])

    second_call_messages = provider_mgr.ask.call_args_list[1][0][0]
    tool_obs = [m for m in second_call_messages if m.get("role") == "tool"][0]
    assert "[... output truncated by Kira ...]" in tool_obs["content"]
    assert len(tool_obs["content"]) < 5000


# ---------------------------------------------------------------------------
# Retries strictly on execution errors (never blocks or denials)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retries_on_policy_block():
    call = DummyToolCall("blk_1", "format_disk", {})
    msg_blk = DummyMessage(tool_calls=[call])
    msg_final = DummyMessage(content="Blocked.")

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock(side_effect=[
        (msg_blk, "groq"),
        (msg_final, "groq"),
    ])

    runner = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=ModeManager(),
    )
    with patch.object(runner, "_execute_tool_with_retries") as mock_exec:
        await runner.run([{"role": "user", "content": "format disk"}])
        mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_no_retries_on_user_denial():
    call = DummyToolCall("deny_1", "take_screenshot", {})
    msg_ask = DummyMessage(tool_calls=[call])
    msg_final = DummyMessage(content="Declined.")

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock(side_effect=[
        (msg_ask, "groq"),
        (msg_final, "groq"),
    ])

    runner = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=ModeManager(),
        confirm_callback=lambda _: False,
    )
    with patch.object(runner, "_execute_tool_with_retries") as mock_exec:
        await runner.run([{"role": "user", "content": "take screenshot"}])
        mock_exec.assert_not_called()


# ---------------------------------------------------------------------------
# Actions logged to audit and emitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actions_logged_to_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import backend.audit as audit_module

    audit_file = tmp_path / "test_actions.jsonl"
    monkeypatch.setattr(audit_module, "ACTIONS_LOG", audit_file)
    monkeypatch.setattr(audit_module, "LOGS_DIR", tmp_path)

    emitted_events: list[dict] = []

    def mock_listener(evt: dict) -> None:
        emitted_events.append(evt)

    audit_module.register_listener(mock_listener)
    try:
        call = DummyToolCall("aud_1", "speak", {"text": "hello audit"})
        msg_tool = DummyMessage(tool_calls=[call])
        msg_final = DummyMessage(content="Finished audit.")

        provider_mgr = MagicMock(spec=ProviderManager)
        provider_mgr.ask = AsyncMock(side_effect=[
            (msg_tool, "groq"),
            (msg_final, "groq"),
        ])

        runner = AgentRunner(
            provider_manager=provider_mgr,
            mode_manager=ModeManager(),
        )

        await runner.run([{"role": "user", "content": "test audit"}])

        assert len(emitted_events) >= 1
        assert emitted_events[0]["tool"] == "speak"
        assert emitted_events[0]["decision"] == "allow"
        assert "hello audit" in str(emitted_events[0]["args"])

        assert audit_file.exists()
        lines = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").strip().splitlines()]
        assert len(lines) >= 1
        assert lines[0]["tool"] == "speak"
    finally:
        audit_module.remove_listener(mock_listener)


# ---------------------------------------------------------------------------
# Provider switch to Ollama forces Restricted mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_switch_to_ollama_notifies():
    msg_final = DummyMessage(content="Ollama answering")

    provider_mgr = MagicMock(spec=ProviderManager)
    provider_mgr.ask = AsyncMock(return_value=(msg_final, "ollama"))

    mode_mgr = ModeManager()
    # Put into FREE mode
    mode_mgr._mode = Mode.FREE

    runner = AgentRunner(
        provider_manager=provider_mgr,
        mode_manager=mode_mgr,
    )

    reply, provider = await runner.run([{"role": "user", "content": "hi"}])
    assert provider == "ollama"
    assert runner.mode_manager.mode == Mode.RESTRICTED
