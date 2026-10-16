"""
test_server.py
==============
Unit tests for Kira WebSocket Server (backend/server.py).

Tests Covered:
- Token file stored outside workspace/ and rejected if inside workspace/
- Token generation, storage, and cleanup on exit
- Host header validation (rejected if not 127.0.0.1/localhost)
- Browser origin rejection (http/https origins rejected)
- 'null' origin without valid token rejected
- Auth timeout when no auth message is sent
- Stop command activates KILL_SWITCH
- Yes confirmation with question_id reaches shared_confirm hook
- Stale or mismatched question_id is ignored
- Free mode activation refused over WebSocket
- Invalid JSON / unknown command safely ignored
- Slow client queue overflow dropped
- Outbound event whitelist schema: secrets stripped, phone numbers masked, summaries truncated
- Loudness event throttling (~20/sec)
- Publishing from non-asyncio thread reaches WebSocket client
- Port-in-use handled gracefully (running=False, no crash)
- Initial state and mode snapshot sent on connect
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

import backend.events as events_module
from backend.policy import KILL_SWITCH, Mode, ModeManager
from backend.server import WebSocketServer, mask_phone_numbers, truncate_text
from backend.voice import (
    get_active_question_id,
    register_confirmation_response,
)


def get_free_port() -> int:
    """Find an available port on 127.0.0.1 for testing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def reset_kill_switch():
    KILL_SWITCH.reset()
    yield
    KILL_SWITCH.reset()


@pytest.fixture
def server_harness(tmp_path: Path):
    """
    Start a live WebSocketServer on an ephemeral port in its own thread,
    and cleanly tear it down after each test.
    """
    port = get_free_port()
    token_file = tmp_path / ".kira_test" / "token"
    mode_mgr = ModeManager()

    config = {
        "server": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": port,
            "token_file": str(token_file),
            "max_clients": 3,
            "max_message_size": 4096,
            "inbound_rate_limit": 20,
            "loudness_rate_limit": 20,
            "allowed_origins": ["null", "file://", "vscode-webview://", "electron://"],
        }
    }

    server = WebSocketServer(config=config, mode_manager=mode_mgr)
    server.start_in_thread()

    # Wait until server is listening
    deadline = time.monotonic() + 5.0
    while not server.running and time.monotonic() < deadline:
        time.sleep(0.05)

    assert server.running, "Test WebSocket server failed to start within 5s"

    token = token_file.read_text(encoding="utf-8").strip()

    yield {
        "server": server,
        "host": "127.0.0.1",
        "port": port,
        "token": token,
        "token_file": token_file,
        "mode_manager": mode_mgr,
        "uri": f"ws://127.0.0.1:{port}",
    }

    server.stop()
    assert not token_file.exists(), "Token file should be deleted on stop"


# =============================================================================
# 1. Token Storage & Security Boundary Tests
# =============================================================================


def test_token_file_not_under_workspace(tmp_path: Path):
    """Token file must be stored outside workspace/ and raises ValueError if attempted."""
    workspace_dir = Path(__file__).parent.parent / "workspace"
    illegal_token_file = workspace_dir / "forbidden_token"

    cfg = {
        "server": {
            "token_file": str(illegal_token_file),
        }
    }

    with pytest.raises(ValueError, match="Token file cannot be inside workspace/"):
        WebSocketServer(config=cfg)


def test_token_file_created_and_deleted_on_stop(server_harness):
    """Verify ephemeral token lifecycle: created at start, deleted on stop."""
    token_file = server_harness["token_file"]
    assert token_file.exists()
    assert len(server_harness["token"]) >= 32

    # Stop and verify deletion
    server_harness["server"].stop()
    assert not token_file.exists()


# =============================================================================
# 2. Host and Origin Validation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_host_header_validation(server_harness):
    """Reject connection with spoofed/invalid Host header."""
    uri = server_harness["uri"]
    with pytest.raises(ConnectionClosed) as exc_info:
        async with connect(
            uri,
            additional_headers={"Host": "evil-website.com"},
        ) as ws:
            await ws.recv()

    assert exc_info.value.rcvd.code == 1008


@pytest.mark.asyncio
async def test_browser_origin_rejected(server_harness):
    """Web browser origins (http://, https://) must always be rejected."""
    uri = server_harness["uri"]
    for bad_origin in ["http://malicious.com", "https://evil.site", "http://localhost:3000"]:
        with pytest.raises(ConnectionClosed) as exc_info:
            async with connect(
                uri,
                additional_headers={"Origin": bad_origin},
            ) as ws:
                await ws.recv()
        assert exc_info.value.rcvd.code == 1008


@pytest.mark.asyncio
async def test_null_origin_wrong_token_rejected(server_harness):
    """'null' origin without valid token is closed with 1008."""
    uri = server_harness["uri"]
    with pytest.raises(ConnectionClosed) as exc_info:
        async with connect(uri, additional_headers={"Origin": "null"}) as ws:
            # Send wrong token
            await ws.send(json.dumps({"type": "auth", "token": "invalid_token_12345"}))
            await ws.recv()

    assert exc_info.value.rcvd.code == 1008


@pytest.mark.asyncio
async def test_auth_timeout(server_harness):
    """Unauthenticated client is disconnected if no auth message is sent."""
    uri = server_harness["uri"]
    # We patch wait_for timeout to 0.2s to test quickly
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
        with pytest.raises(ConnectionClosed) as exc_info:
            async with connect(uri, additional_headers={"Origin": "null"}) as ws:
                await ws.recv()
        assert exc_info.value.rcvd.code == 1008


# =============================================================================
# 3. Successful Connection, Snapshot, and Outbound Whitelist Schema
# =============================================================================


@pytest.mark.asyncio
async def test_connect_auth_and_snapshot(server_harness):
    """Authenticated client receives auth_ok and initial state snapshot."""
    uri = server_harness["uri"]
    token = server_harness["token"]

    async with connect(uri, additional_headers={"Origin": "null"}) as ws:
        # Send auth
        await ws.send(json.dumps({"type": "auth", "token": token}))

        # Receive auth_ok
        msg1 = json.loads(await ws.recv())
        assert msg1.get("type") == "auth_ok"
        assert msg1.get("status") == "authenticated"

        # Receive snapshot
        msg2 = json.loads(await ws.recv())
        assert msg2.get("type") == "snapshot"
        assert msg2.get("state") == "idle"
        assert msg2.get("mode") == "restricted"
        assert "timestamp" in msg2


@pytest.mark.asyncio
async def test_outbound_whitelist_schema_and_pii_masking(server_harness):
    """Outbound events contain only whitelisted fields, masked phones, and no secrets."""
    uri = server_harness["uri"]
    token = server_harness["token"]

    async with connect(uri, additional_headers={"Origin": "null"}) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        await ws.recv()  # auth_ok
        await ws.recv()  # snapshot

        # Publish an event with secrets, phone numbers, and extra metadata
        events_module.publish(
            "agent.step_done",
            "Executed action successfully for customer at 555-123-4567",
            {
                "step": 2,
                "tool": "create_file",
                "summary": "Created file with secret key=sk-12345 and phone 555-987-6543",
                "api_key": "SUPER_SECRET_KEY",
                "password": "DO_NOT_LEAK",
                "file_contents": "raw contents here",
            },
        )

        msg = json.loads(await ws.recv())
        assert msg["type"] == "step_done"
        assert msg["step"] == 2
        assert msg["tool"] == "create_file"
        # Secrets stripped
        assert "api_key" not in msg
        assert "password" not in msg
        assert "file_contents" not in msg
        # Phone masked
        assert "555-987-6543" not in msg["summary"]
        assert "[PHONE]" in msg["summary"]
        # Summary truncated <= 120 chars
        assert len(msg["summary"]) <= 120
        # Whitelisted keys only
        assert set(msg.keys()) == {"type", "step", "tool", "summary", "timestamp"}


# =============================================================================
# 4. Inbound Commands: Stop, Confirm (question_id), Restricted, Free Mode
# =============================================================================


@pytest.mark.asyncio
async def test_stop_command_sets_kill_switch(server_harness):
    """Sending 'stop' command sets the KILL_SWITCH immediately."""
    uri = server_harness["uri"]
    token = server_harness["token"]

    assert not KILL_SWITCH.is_stopped()

    async with connect(uri, additional_headers={"Origin": "null"}) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        await ws.recv()  # auth_ok
        await ws.recv()  # snapshot

        # Send stop command
        await ws.send(json.dumps({"command": "stop"}))
        resp = json.loads(await ws.recv())
        assert resp["type"] == "command_ack"
        assert resp["command"] == "stop"
        assert resp["status"] == "executed"

        assert KILL_SWITCH.is_stopped()


@pytest.mark.asyncio
async def test_yes_confirmation_with_question_id(server_harness):
    """A yes confirmation matching the pending question_id is accepted."""
    import queue
    import backend.voice as voice_module

    test_q_id = "testq123"
    test_queue = queue.Queue()

    with voice_module._active_confirm_lock:
        voice_module._active_confirm_question_id = test_q_id
        voice_module._active_confirm_queue = test_queue

    try:
        uri = server_harness["uri"]
        token = server_harness["token"]

        async with connect(uri, additional_headers={"Origin": "null"}) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token}))
            await ws.recv()  # auth_ok
            await ws.recv()  # snapshot

            # Send confirm with matching question_id
            await ws.send(
                json.dumps({"command": "confirm", "question_id": test_q_id, "value": "yes"})
            )
            ack = json.loads(await ws.recv())
            assert ack["type"] == "command_ack"
            assert ack["status"] == "accepted"

            # Check that test_queue received the response
            src, val = test_queue.get_nowait()
            assert src == "websocket"
            assert val == "yes"
    finally:
        with voice_module._active_confirm_lock:
            voice_module._active_confirm_question_id = None
            voice_module._active_confirm_queue = None


@pytest.mark.asyncio
async def test_stale_or_missing_question_id_ignored(server_harness):
    """Mismatched or stale question_id is ignored and not queued."""
    import queue
    import backend.voice as voice_module

    test_q_id = "current1"
    test_queue = queue.Queue()

    with voice_module._active_confirm_lock:
        voice_module._active_confirm_question_id = test_q_id
        voice_module._active_confirm_queue = test_queue

    try:
        uri = server_harness["uri"]
        token = server_harness["token"]

        async with connect(uri, additional_headers={"Origin": "null"}) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token}))
            await ws.recv()  # auth_ok
            await ws.recv()  # snapshot

            # Send confirm with stale question_id
            await ws.send(
                json.dumps({"command": "confirm", "question_id": "stale999", "value": "yes"})
            )
            ack = json.loads(await ws.recv())
            assert ack["type"] == "command_ack"
            assert ack["status"] == "ignored"

            # Verify queue remains empty
            assert test_queue.empty()
    finally:
        with voice_module._active_confirm_lock:
            voice_module._active_confirm_question_id = None
            voice_module._active_confirm_queue = None


@pytest.mark.asyncio
async def test_free_mode_refused(server_harness):
    """Free mode cannot be enabled via WebSocket and returns an explicit error."""
    uri = server_harness["uri"]
    token = server_harness["token"]
    mode_mgr = server_harness["mode_manager"]

    assert mode_mgr.mode == Mode.RESTRICTED

    async with connect(uri, additional_headers={"Origin": "null"}) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        await ws.recv()  # auth_ok
        await ws.recv()  # snapshot

        await ws.send(json.dumps({"command": "free_mode"}))
        resp = json.loads(await ws.recv())
        assert resp["type"] == "error"
        assert resp["code"] == "free_mode_forbidden"

        # Mode remains restricted
        assert mode_mgr.mode == Mode.RESTRICTED


@pytest.mark.asyncio
async def test_invalid_message_ignored(server_harness):
    """Malformed JSON or unknown commands are safely ignored without crashing."""
    uri = server_harness["uri"]
    token = server_harness["token"]

    async with connect(uri, additional_headers={"Origin": "null"}) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        await ws.recv()  # auth_ok
        await ws.recv()  # snapshot

        # Malformed JSON string
        await ws.send("NOT_A_JSON_STRING{{{")
        # Unknown command
        await ws.send(json.dumps({"command": "dance_a_jig"}))

        # Server is still responsive
        await ws.send(json.dumps({"command": "restricted_mode"}))
        resp = json.loads(await ws.recv())
        assert resp["type"] == "command_ack"
        assert resp["command"] == "restricted_mode"


# =============================================================================
# 5. Robustness: Slow Clients, Thread-Safe Publish, Loudness Throttling
# =============================================================================


@pytest.mark.asyncio
async def test_slow_client_dropped(server_harness):
    """A client with an overflowing queue is dropped without crashing the server."""
    uri = server_harness["uri"]
    token = server_harness["token"]

    async with connect(uri, additional_headers={"Origin": "null"}) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        await ws.recv()  # auth_ok
        await ws.recv()  # snapshot

        # Cancel writer task to simulate stalled network/consumer
        server = server_harness["server"]
        for ws_conn, task in list(server._client_tasks.items()):
            task.cancel()

        # Flood 60 events to overflow the maxsize=50 queue
        for i in range(60):
            events_module.publish("agent.step_start", f"Step {i}", {"step": i, "tool": "test"})

        await asyncio.sleep(0.1)

        # The client should be closed by server with code 1011 (slow consumer)
        with pytest.raises(ConnectionClosed) as exc_info:
            await ws.recv()

        assert exc_info.value.rcvd.code == 1011


@pytest.mark.asyncio
async def test_publish_from_non_asyncio_thread(server_harness):
    """Events published from external worker threads arrive safely on the socket."""
    uri = server_harness["uri"]
    token = server_harness["token"]

    async with connect(uri, additional_headers={"Origin": "null"}) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        await ws.recv()  # auth_ok
        await ws.recv()  # snapshot

        # Publish from background thread
        def thread_publisher():
            events_module.publish(
                "voice.speaking",
                "Spoken from another thread",
                {"text": "Hello from background thread"},
            )

        th = threading.Thread(target=thread_publisher)
        th.start()
        th.join()

        msg = json.loads(await ws.recv())
        assert msg["type"] == "speech"
        assert "Hello from background thread" in msg["text"]


@pytest.mark.asyncio
async def test_loudness_throttled(server_harness):
    """Rapid loudness events are throttled to ~20/sec."""
    uri = server_harness["uri"]
    token = server_harness["token"]

    async with connect(uri, additional_headers={"Origin": "null"}) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        await ws.recv()  # auth_ok
        await ws.recv()  # snapshot

        # Fire 30 loudness events in 0.05 seconds
        for i in range(30):
            events_module.publish("voice.loudness", "level", {"level": i / 30.0})

        # Allow brief time for dispatcher
        await asyncio.sleep(0.1)

        # Count how many loudness events were received
        count = 0
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                ev = json.loads(raw)
                if ev.get("type") == "loudness":
                    count += 1
            except asyncio.TimeoutError:
                break

        # Throttling must ensure count is significantly less than 30 (e.g. <= 4)
        assert count <= 4, f"Expected <= 4 loudness events due to throttling, got {count}"


# =============================================================================
# 6. Port In Use Graceful Degradation
# =============================================================================


def test_port_in_use_handled_gracefully(tmp_path: Path):
    """If port is occupied, server logs warning, sets running=False, and does not crash."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    occupied_port = sock.getsockname()[1]

    cfg = {
        "server": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": occupied_port,
            "token_file": str(tmp_path / "token_port_conflict"),
        }
    }

    server = WebSocketServer(config=cfg)
    try:
        # Starting in thread should handle OSError and not raise
        server.start_in_thread()
        assert server.running is False
    finally:
        server.stop()
        sock.close()
