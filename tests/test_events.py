"""
test_events.py
==============
Unit tests for Kira publish/subscribe event bus (backend/events.py).

Covers:
- Subscriber callbacks received synchronously
- Multiple subscribers and unsubscription
- Event ordering preservation
- Automatic stripping of sensitive keys (key, token, password, secret)
- JSONL log generation in logs/events.jsonl
- Full agent flow runs in Restricted mode with no user confirmation prompt
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend import events
from backend.agent import AgentRunner
from backend.policy import Mode, ModeManager
from backend.providers import ProviderManager


@pytest.fixture(autouse=True)
def clean_subscribers():
    """Ensure subscriber list is clean before and after each test."""
    events._subscribers.clear()
    yield
    events._subscribers.clear()


def test_publish_calls_subscriber():
    received = []

    def on_event(ev: dict):
        received.append(ev)

    events.subscribe(on_event)
    events.publish("test.event", "Hello world", {"count": 1})

    assert len(received) == 1
    assert received[0]["type"] == "test.event"
    assert received[0]["text"] == "Hello world"
    assert received[0]["data"] == {"count": 1}
    assert "ts" in received[0]


def test_unsubscribe_stops_callbacks():
    received = []

    def on_event(ev: dict):
        received.append(ev)

    events.subscribe(on_event)
    events.publish("test.first", "First")
    assert len(received) == 1

    events.unsubscribe(on_event)
    events.publish("test.second", "Second")
    assert len(received) == 1  # No new event received


def test_event_ordering():
    order = []

    def on_event(ev: dict):
        order.append(ev["type"])

    events.subscribe(on_event)
    events.publish("step.1", "First")
    events.publish("step.2", "Second")
    events.publish("step.3", "Third")

    assert order == ["step.1", "step.2", "step.3"]


def test_no_secrets_in_events():
    received = []

    events.subscribe(lambda ev: received.append(ev))
    events.publish(
        "test.security",
        "Checking secrets",
        {
            "safe_param": "hello",
            "api_key": "secret-123",
            "token": "bearer-456",
            "password": "pass",
            "jwt_secret": "xyz",
            "apikey": "abc",
        },
    )

    data = received[0]["data"]
    assert "safe_param" in data
    assert "api_key" not in data
    assert "token" not in data
    assert "password" not in data
    assert "jwt_secret" not in data
    assert "apikey" not in data


def test_jsonl_log_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log_file = tmp_path / "test_events.jsonl"
    monkeypatch.setattr(events, "EVENTS_LOG", log_file)
    monkeypatch.setattr(events, "LOGS_DIR", tmp_path)

    events.publish("log.test", "A log line", {"n": 42})

    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["type"] == "log.test"
    assert parsed["text"] == "A log line"
    assert parsed["data"]["n"] == 42


@pytest.mark.asyncio
async def test_demo_no_confirm_in_restricted():
    """
    Ensure the canonical demo tools (create_folder, open_in_vscode, run_command)
    run in Restricted mode without triggering any user confirmation prompts.
    """
    mode_mgr = ModeManager()
    assert mode_mgr.mode == Mode.RESTRICTED

    mock_provider = MagicMock(spec=ProviderManager)

    # First turn: model calls open_in_vscode and run_command
    tool_call_1 = MagicMock()
    tool_call_1.id = "call_1"
    tool_call_1.function.name = "open_in_vscode"
    tool_call_1.function.arguments = json.dumps({"path": "demo", "code": "print('hello')", "filename": "hello.py"})

    tool_call_2 = MagicMock()
    tool_call_2.id = "call_2"
    tool_call_2.function.name = "run_command"
    tool_call_2.function.arguments = json.dumps({"command": "python hello.py", "cwd": "demo"})

    turn1_msg = MagicMock()
    turn1_msg.content = "I will create demo/hello.py and run it."
    turn1_msg.tool_calls = [tool_call_1, tool_call_2]

    # Second turn: final report
    turn2_msg = MagicMock()
    turn2_msg.content = "Here is the output in the terminal: hello"
    turn2_msg.tool_calls = None

    mock_provider.ask = AsyncMock(side_effect=[
        (turn1_msg, "groq"),
        (turn2_msg, "groq"),
    ])

    confirm_mock = MagicMock(return_value=True)

    agent = AgentRunner(
        provider_manager=mock_provider,
        mode_manager=mode_mgr,
        confirm_callback=confirm_mock,
    )

    with patch("backend.agent.TOOL_FUNCTIONS", {
        "open_in_vscode": MagicMock(return_value="File created and typed"),
        "run_command": MagicMock(return_value="hello"),
    }):
        with patch("backend.tools.execution.inspect_command", return_value=(False, True, ["python", "hello.py"])):
            reply, prov = await agent.run([{"role": "user", "content": "Run demo"}])

    # Confirmation callback must NOT be called for allowlisted actions inside workspace
    assert confirm_mock.call_count == 0
    assert "Here is the output in the terminal: hello" in reply
    assert prov == "groq"
