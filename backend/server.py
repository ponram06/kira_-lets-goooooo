"""
server.py
=========
WebSocket server for Kira (Step 6).

Streams the in-process event bus (backend/events.py) to connected desktop
clients (e.g. Electron 3D avatar, test scripts) as structured JSON.
Runs on a dedicated background thread with its own asyncio event loop so
terminal input() and tool executions never block the server.

Security & Architecture
-----------------------
- Binds strictly to 127.0.0.1 (local loopback).
- Ephemeral auth token generated via secrets.token_urlsafe(32) and stored
  outside workspace/ (e.g. .kira/token), deleted on exit.
- Auth: token is NOT in the URL. Client must send auth as its first message
  within 5 seconds; tokens compared with secrets.compare_digest.
- Host validation: Host header must be 127.0.0.1:{port} or localhost:{port}.
- Origin validation: Web http(s) origins are strictly rejected. Local origins
  ("null", file://) and non-browser clients require a valid token.
- Outbound whitelist schema: only designated fields per event type are sent;
  summaries truncated (<=120 chars), phone numbers masked, zero keys/secrets.
- Loudness throttled to ~20/sec, latest value wins.
- Bounded client queues (50 msgs max): slow consumers dropped without blocking.
- Inbound rate limits: max 20 msgs/sec per client, max 4 KB frame size.
- Graceful degradation: if port is in use or server.enabled is false, logs
  a warning and Kira continues without crashing.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Callable, Dict, Optional, Set

import yaml
from websockets.asyncio.server import ServerConnection, serve

from backend import events as _events
from backend import secrets_guard
from backend.policy import KILL_SWITCH, ModeManager
from backend.voice import register_confirmation_response

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).parent.parent / "config.yaml"
_ROOT_DIR = Path(__file__).parent.parent

# Regex to mask phone numbers in outbound event text
_PHONE_PATTERN = re.compile(r"(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")


def mask_phone_numbers(text: str) -> str:
    """Mask phone numbers to prevent PII leakage."""
    return _PHONE_PATTERN.sub("[PHONE]", text)


def truncate_text(text: str, max_len: int = 120) -> str:
    """Mask PII and truncate text safely."""
    if not text:
        return ""
    masked = mask_phone_numbers(text)
    return masked[:max_len]


class WebSocketServer:
    """
    Kira WebSocket server daemon streaming events and accepting remote control commands.
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        config_path: Path = _DEFAULT_CONFIG,
        mode_manager: Optional[ModeManager] = None,
        voice_manager: Optional[Any] = None,
        app_state: Optional[Any] = None,
    ) -> None:
        if config is None:
            config = self._load_config(config_path)

        server_cfg = config.get("server", {})
        self.enabled: bool = bool(server_cfg.get("enabled", True))
        self.host: str = server_cfg.get("host", "127.0.0.1")
        self.port: int = int(server_cfg.get("port", 8765))

        # Token path strictly outside workspace/
        raw_token_path = server_cfg.get("token_file", ".kira/token")
        p = Path(raw_token_path)
        if not p.is_absolute():
            self.token_file: Path = (_ROOT_DIR / p).resolve()
        else:
            self.token_file = p.resolve()

        # Guard: Token file MUST NOT be inside workspace/
        workspace_dir = (_ROOT_DIR / "workspace").resolve()
        try:
            self.token_file.relative_to(workspace_dir)
            raise ValueError(f"Security error: Token file cannot be inside workspace/: {self.token_file}")
        except ValueError as exc:
            if "Security error" in str(exc):
                raise
            # Expected: not relative to workspace

        self.max_clients: int = int(server_cfg.get("max_clients", 5))
        self.max_message_size: int = int(server_cfg.get("max_message_size", 4096))
        self.inbound_rate_limit: int = int(server_cfg.get("inbound_rate_limit", 20))
        self.loudness_rate_limit: int = int(server_cfg.get("loudness_rate_limit", 20))
        self.allowed_origins: list[str] = server_cfg.get(
            "allowed_origins", ["null", "file://", "vscode-webview://", "electron://"]
        )

        self.mode_manager = mode_manager
        self.voice_manager = voice_manager
        self.app_state = app_state

        self.token: str = ""
        self.running: bool = False
        self._server = None
        self._server_task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        # Connected authenticated clients and their bounded outbound queues
        self._clients: Dict[ServerConnection, asyncio.Queue] = {}
        self._client_tasks: Dict[ServerConnection, asyncio.Task] = {}

        # Internal event processing queue for threadsafe publish
        self._event_queue: Optional[asyncio.Queue] = None

        # Current state caching for snapshot
        self._current_state: str = "idle"
        self._current_mode: str = "restricted"
        if self.mode_manager:
            self._current_mode = self.mode_manager.mode.value

        # Loudness throttle state
        self._last_loudness_time: float = 0.0
        self._loudness_min_interval: float = 1.0 / max(1, self.loudness_rate_limit)

        # Register cleanup on program exit
        atexit.register(self.cleanup_token)

    @staticmethod
    def _load_config(path: Path) -> dict:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
        return {}

    def _generate_and_save_token(self) -> None:
        """Generate a random 32-byte urlsafe token and store it outside workspace/."""
        self.token = secrets.token_urlsafe(32)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(self.token, encoding="utf-8")
        logger.info("Ephemeral WebSocket token written to %s", self.token_file)

    def cleanup_token(self) -> None:
        """Delete ephemeral token file on server shutdown or process exit."""
        if self.token_file and self.token_file.exists():
            try:
                self.token_file.unlink()
                logger.info("Ephemeral WebSocket token removed")
            except OSError:
                pass

    def get_snapshot(self) -> dict:
        """Return the current system snapshot."""
        mode_val = self.mode_manager.mode.value if self.mode_manager else self._current_mode
        snap = {
            "type": "snapshot",
            "state": self._current_state,
            "mode": mode_val,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if self.app_state is not None:
            snap.update(self.app_state.status())
        elif self.mode_manager is not None:
            snap["free_mode_available"] = self.mode_manager.free_mode_available
        return snap

    # -------------------------------------------------------------------------
    # Server Lifecycle
    # -------------------------------------------------------------------------

    def start_in_thread(self) -> None:
        """
        Launch the WebSocket server on a dedicated background thread with its
        own asyncio event loop so REPL input() never blocks the server.
        """
        if not self.enabled:
            logger.info("WebSocket server disabled in config.")
            return

        ready_event = threading.Event()

        def _worker():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self._event_queue = asyncio.Queue()

            try:
                self.loop.run_until_complete(self._async_start(ready_event))
            except Exception as exc:
                logger.error("Error in WebSocket server loop: %s", exc)
                ready_event.set()
                return

            try:
                self.loop.run_forever()
            finally:
                self.loop.run_until_complete(self._async_stop())
                self.loop.close()

        self._thread = threading.Thread(target=_worker, name="KiraWebSocketServer", daemon=True)
        self._thread.start()
        ready_event.wait(timeout=5.0)

    async def start(self) -> None:
        """Start the server directly on the current running event loop."""
        if not self.enabled:
            return
        self.loop = asyncio.get_running_loop()
        self._event_queue = asyncio.Queue()
        ready_event = threading.Event()
        await self._async_start(ready_event)

    async def _async_start(self, ready_event: threading.Event) -> None:
        """Internal async startup."""
        self._generate_and_save_token()

        try:
            self._server = await serve(
                self._handle_connection,
                self.host,
                self.port,
                max_size=self.max_message_size,
                ping_interval=20,
                ping_timeout=10,
            )
            self.running = True
            logger.info("WebSocket server running on ws://%s:%d", self.host, self.port)

            # Subscribe to the in-process event bus
            _events.subscribe(self._on_bus_event)

            # Start event dispatcher task
            self._server_task = asyncio.create_task(self._event_dispatcher_loop())

        except OSError as exc:
            logger.warning(
                "WebSocket server failed to bind to %s:%d (%s). Continuing without server.",
                self.host,
                self.port,
                exc,
            )
            self.running = False
            self.cleanup_token()
        finally:
            ready_event.set()

    def stop(self) -> None:
        """Synchronous shutdown from any thread."""
        if not self.running and not self._server:
            self.cleanup_token()
            return

        self.running = False
        _events.unsubscribe(self._on_bus_event)

        if self.loop and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._async_stop(), self.loop)
            try:
                future.result(timeout=3.0)
            except Exception:
                pass

        if self._thread and self._thread.is_alive() and threading.current_thread() != self._thread:
            self._thread.join(timeout=2.0)

        self.cleanup_token()

    async def _async_stop(self) -> None:
        """Async teardown of all connections and listener."""
        self.running = False
        _events.unsubscribe(self._on_bus_event)

        # Cancel all client writer tasks
        for task in list(self._client_tasks.values()):
            task.cancel()
        self._client_tasks.clear()

        # Close all active client connections
        for client in list(self._clients.keys()):
            try:
                await client.close(code=1001, reason="Server shutting down")
            except Exception:
                pass
        self._clients.clear()

        if self._server_task:
            self._server_task.cancel()
            self._server_task = None

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -------------------------------------------------------------------------
    # Thread-Safe Event Subscription & Broadcasting
    # -------------------------------------------------------------------------

    def _on_bus_event(self, event: dict[str, Any]) -> None:
        """
        Synchronous callback called on ANY publishing thread.
        Routes event to the server's asyncio loop using call_soon_threadsafe.
        """
        if not self.running or not self.loop or not self._event_queue:
            return

        try:
            self.loop.call_soon_threadsafe(self._event_queue.put_nowait, event)
        except Exception:
            pass

    async def _event_dispatcher_loop(self) -> None:
        """Background task that pulls events from _event_queue and broadcasts them."""
        while self.running:
            try:
                event = await self._event_queue.get()
                self._process_and_broadcast(event)
                self._event_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Error in event dispatcher: %s", exc)

    def _process_and_broadcast(self, event: dict[str, Any]) -> None:
        """Filter, format against whitelist schema, throttle, and broadcast."""
        event_type = event.get("type", "")
        data = event.get("data") or {}
        raw_text = event.get("text", "")
        ts = event.get("ts") or datetime.now(timezone.utc).isoformat()

        formatted: Optional[dict[str, Any]] = None

        if event_type == "state":
            st = str(data.get("state") or raw_text)
            self._current_state = st
            formatted = {"type": "state", "state": st, "timestamp": ts}

        elif event_type == "agent.step_start":
            tool_name = str(data.get("tool", ""))
            self._current_state = "working"
            formatted = {
                "type": "step_start",
                "step": int(data.get("step", 0)),
                "tool": tool_name,
                "timestamp": ts,
            }

        elif event_type == "agent.step_done":
            summary = truncate_text(str(data.get("summary") or raw_text), 120)
            formatted = {
                "type": "step_done",
                "step": int(data.get("step", 0)),
                "tool": str(data.get("tool", "")),
                "summary": summary,
                "timestamp": ts,
            }

        elif event_type == "tool.start":
            self._current_state = "working"
            formatted = {
                "type": "tool_start",
                "tool": str(data.get("tool", "")),
                "timestamp": ts,
            }

        elif event_type == "tool.done":
            summary = truncate_text(str(data.get("summary") or raw_text), 120)
            formatted = {
                "type": "tool_done",
                "tool": str(data.get("tool", "")),
                "summary": summary,
                "timestamp": ts,
            }

        elif event_type == "policy.ask":
            prompt = mask_phone_numbers(str(data.get("prompt") or raw_text))
            qid = str(data.get("question_id", ""))
            formatted = {
                "type": "policy_ask",
                "question_id": qid,
                "prompt": prompt,
                "timestamp": ts,
            }

        elif event_type == "mode.change":
            mode_val = str(data.get("mode") or "restricted")
            self._current_mode = mode_val
            formatted = {"type": "mode_change", "mode": mode_val, "timestamp": ts}

        elif event_type == "voice.listening":
            self._current_state = "listening"
            formatted = {"type": "state", "state": "listening", "timestamp": ts}

        elif event_type == "voice.processing":
            self._current_state = "thinking"
            formatted = {"type": "state", "state": "thinking", "timestamp": ts}

        elif event_type == "voice.speaking":
            self._current_state = "speaking"
            # Whitelist: only spoken text is sent, never raw files/secrets
            speech_text = truncate_text(str(data.get("text") or raw_text), 200)
            formatted = {"type": "speech", "text": speech_text, "timestamp": ts}

        elif event_type == "voice.done":
            self._current_state = "idle"
            formatted = {"type": "state", "state": "idle", "timestamp": ts}

        elif event_type == "voice.loudness":
            now = time.monotonic()
            if (now - self._last_loudness_time) < self._loudness_min_interval:
                # Throttle loudness events to ~20/sec
                return
            self._last_loudness_time = now
            raw_lvl = data.get("level", data.get("loudness", data.get("value", 0.0)))
            lvl = max(0.0, min(1.0, round(float(raw_lvl), 3)))
            formatted = {"type": "loudness", "level": lvl, "value": lvl, "timestamp": ts}

        elif event_type == "policy.decision":
            decision = str(data.get("decision", ""))
            # Only blocks and countdowns are interesting for the action feed
            if decision in ("block", "countdown"):
                formatted = {
                    "type": "policy_decision",
                    "decision": decision,
                    "tool": str(data.get("tool", "")),
                    "timestamp": ts,
                }

        elif event_type == "tool.error":
            formatted = {"type": "tool_error", "summary": truncate_text(raw_text, 120), "timestamp": ts}

        elif event_type == "kill_switch.activated":
            formatted = {"type": "kill_switch", "timestamp": ts}

        elif event_type == "free_mode.denied":
            formatted = {
                "type": "free_mode_denied",
                "reason": str(data.get("reason", "")),
                "lockout_seconds": int(data.get("lockout_seconds", 0) or 0),
                "timestamp": ts,
            }

        elif event_type == "persona.change":
            formatted = {"type": "persona_change", "persona": str(data.get("persona", "")), "timestamp": ts}

        elif event_type == "mic.mute":
            formatted = {"type": "mic_mute", "muted": bool(data.get("muted")), "timestamp": ts}

        elif event_type == "sleep.change":
            formatted = {"type": "sleep_change", "sleeping": bool(data.get("sleeping")), "timestamp": ts}

        elif event_type == "policy.countdown":
            formatted = {
                "type": "countdown",
                "label": truncate_text(str(data.get("label", "")), 80),
                "seconds_left": int(data.get("seconds_left", 0) or 0),
                "cancelled": bool(data.get("cancelled", False)),
                "timestamp": ts,
            }

        elif event_type == "reminder.due":
            formatted = {
                "type": "reminder",
                "text": truncate_text(str(data.get("text", "")), 200),
                "kind": str(data.get("kind", "reminder")),
                "missed": bool(data.get("missed", False)),
                "timestamp": ts,
            }

        elif event_type == "assistant.reply":
            formatted = {"type": "reply", "text": truncate_text(str(data.get("text", "")), 1000), "timestamp": ts}

        elif event_type == "app.shutdown":
            formatted = {"type": "shutdown", "timestamp": ts}

        if formatted is not None:
            self._broadcast(formatted)

    def _broadcast(self, message_dict: dict[str, Any]) -> None:
        """Enqueue formatted JSON message across all active clients."""
        if not self._clients:
            return

        payload = secrets_guard.redact(json.dumps(message_dict, ensure_ascii=False))
        for ws, queue in list(self._clients.items()):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("Dropping slow WebSocket consumer: %s", ws.remote_address)
                asyncio.create_task(self._drop_client(ws, code=1011, reason="Slow consumer"))

    async def _drop_client(self, websocket: ServerConnection, code: int = 1000, reason: str = "") -> None:
        """Cleanly disconnect a client and discard its worker task."""
        if websocket in self._clients:
            del self._clients[websocket]
        task = self._client_tasks.pop(websocket, None)
        if task:
            task.cancel()
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Client Connection & Security Handshake
    # -------------------------------------------------------------------------

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        """
        Handle incoming client connection with Host, Origin, Rate-limit,
        and Token authentication validation.
        """
        # 1. Validate Host header
        try:
            host_hdrs = websocket.request.headers.get_all("Host")
        except Exception:
            host_hdrs = []

        if len(host_hdrs) != 1:
            logger.warning("Rejected connection with %d Host headers", len(host_hdrs))
            await websocket.close(code=1008, reason="Invalid Host header")
            return

        host_hdr = host_hdrs[0].strip().lower()
        valid_hosts = {
            f"{self.host}:{self.port}".lower(),
            f"localhost:{self.port}".lower(),
            self.host.lower(),
            "localhost",
        }
        if host_hdr not in valid_hosts:
            logger.warning("Rejected connection with invalid Host header: '%s'", host_hdr)
            await websocket.close(code=1008, reason="Invalid Host header")
            return

        # 2. Validate Origin header
        try:
            origins = websocket.request.headers.get_all("Origin")
        except Exception:
            origins = []

        if origins:
            for origin in origins:
                origin_lower = origin.strip().lower()
                # Browser http/https origins are unconditionally rejected
                if origin_lower.startswith("http://") or origin_lower.startswith("https://"):
                    logger.warning("Rejected browser origin: '%s'", origin)
                    await websocket.close(code=1008, reason="Browser origins forbidden")
                    return

                # Check allow-listed origins
                is_allowed = any(
                    origin_lower == ao.lower() or origin_lower.startswith(ao.lower())
                    for ao in self.allowed_origins
                )
                if not is_allowed:
                    logger.warning("Rejected disallowed origin: '%s'", origin)
                    await websocket.close(code=1008, reason="Disallowed origin")
                    return

        # 3. Check client connection limit
        if len(self._clients) >= self.max_clients:
            logger.warning("Rejected connection: max clients (%d) reached", self.max_clients)
            await websocket.close(code=1008, reason="Max clients reached")
            return

        # 4. Require first message auth within 5 seconds
        try:
            auth_msg_raw = await asyncio.wait_for(websocket.recv(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Client timed out waiting for auth message")
            await websocket.close(code=1008, reason="Auth timeout")
            return
        except Exception:
            return

        provided_token = None
        try:
            auth_data = json.loads(auth_msg_raw)
            if isinstance(auth_data, dict):
                provided_token = auth_data.get("token") or auth_data.get("auth")
            elif isinstance(auth_data, str):
                provided_token = auth_data
        except Exception:
            # Not JSON - treat as raw token string directly
            provided_token = str(auth_msg_raw).strip()

        if not provided_token or not secrets.compare_digest(str(provided_token).strip(), self.token):
            logger.warning("Client authentication failed: invalid token")
            await websocket.close(code=1008, reason="Unauthorized")
            return

        # 5. Successfully authenticated!
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=50)
        self._clients[websocket] = queue

        # Start writer task for this client
        writer_task = asyncio.create_task(self._client_writer(websocket, queue))
        self._client_tasks[websocket] = writer_task

        # Send authentication ACK and initial state snapshot
        await websocket.send(json.dumps({"type": "auth_ok", "status": "authenticated"}))
        await websocket.send(json.dumps(self.get_snapshot()))

        # 6. Read loop for incoming commands with rate limiting (max 20 msgs/sec)
        msg_timestamps: collections.deque[float] = collections.deque(maxlen=self.inbound_rate_limit)

        try:
            async for raw_message in websocket:
                now = time.monotonic()
                if len(msg_timestamps) == self.inbound_rate_limit and (now - msg_timestamps[0]) < 1.0:
                    logger.warning("Inbound rate limit exceeded for client %s, dropping frame", websocket.remote_address)
                    continue
                msg_timestamps.append(now)

                await self._handle_client_message(websocket, raw_message)
        except Exception:
            pass
        finally:
            await self._drop_client(websocket)

    async def _client_writer(self, websocket: ServerConnection, queue: asyncio.Queue[str]) -> None:
        """Pulls outbound events from client's queue and sends with 5.0s timeout."""
        try:
            while True:
                msg = await queue.get()
                await asyncio.wait_for(websocket.send(msg), timeout=5.0)
                queue.task_done()
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as exc:
            logger.debug("Client writer encountered error: %s", exc)

    async def _handle_client_message(self, websocket: ServerConnection, raw_message: str) -> None:
        """Process incoming command messages safely against schema."""
        try:
            data = json.loads(raw_message)
            if not isinstance(data, dict):
                return
        except Exception:
            return

        cmd = data.get("command") or data.get("type")
        if not cmd:
            return
        cmd_lower = str(cmd).lower().strip()

        if cmd_lower == "stop":
            KILL_SWITCH.stop()
            if self.voice_manager:
                try:
                    self.voice_manager.stop_speech()
                except Exception:
                    pass
            _events.publish("kill_switch.activated", "Emergency stop triggered via WebSocket")
            await websocket.send(json.dumps({"type": "command_ack", "command": "stop", "status": "executed"}))

        elif cmd_lower == "confirm":
            qid = str(data.get("question_id", ""))
            val = str(data.get("value", data.get("answer", ""))).strip().lower()
            if qid and val in ("yes", "y", "no", "n"):
                accepted = register_confirmation_response(qid, "websocket", val)
                status = "accepted" if accepted else "ignored"
                await websocket.send(
                    json.dumps({
                        "type": "command_ack",
                        "command": "confirm",
                        "question_id": qid,
                        "status": status,
                    })
                )

        elif cmd_lower in ("restricted_mode", "restricted"):
            if self.mode_manager:
                self.mode_manager.enter_restricted()
            await websocket.send(
                json.dumps({"type": "command_ack", "command": "restricted_mode", "status": "executed"})
            )

        elif cmd_lower in ("free_mode", "free"):
            # Free mode needs the PIN dialog (free_mode_request); never a bare command
            await websocket.send(
                json.dumps({
                    "type": "error",
                    "code": "free_mode_forbidden",
                    "message": "Free mode needs the PIN dialog in the avatar menu.",
                })
            )

        elif cmd_lower in self._COMMANDS:
            await self._handle_control_command(websocket, cmd_lower, data)
        # Any other unknown command is safely ignored

    # -------------------------------------------------------------------------
    # Control commands from the tray / avatar menu / settings window (M1)
    # -------------------------------------------------------------------------

    _COMMANDS = frozenset({
        "free_mode_request", "set_persona", "set_mic_muted", "set_sleep",
        "get_status", "reload_settings", "shutdown",
        "user_text", "cancel_countdown",
    })

    async def _handle_control_command(self, websocket: ServerConnection, cmd: str, data: dict) -> None:
        """
        Handle a control command and reply with a command_ack carrying the
        client's request_id. Payload values (especially the PIN) are never
        logged or echoed back.
        """
        ack: dict[str, Any] = {"type": "command_ack", "command": cmd, "status": "executed"}
        rid = data.get("request_id")
        if isinstance(rid, (str, int)) and len(str(rid)) <= 64:
            ack["request_id"] = rid
        st = self.app_state

        if cmd == "free_mode_request":
            pin = data.get("pin")
            if not self.mode_manager or not isinstance(pin, str) or len(pin) > 64:
                ack["status"] = "invalid"
            else:
                result = await asyncio.to_thread(self.mode_manager.try_enter_free, pin)
                ack["status"] = result
                if result == "locked":
                    ack["lockout_seconds"] = round(self.mode_manager.lockout_remaining())
                if result == "ok":
                    ack["free_timeout"] = int(self.mode_manager.free_timeout)
            pin = None

        elif cmd == "set_persona":
            ok = bool(st and st.set_persona(str(data.get("persona", ""))))
            ack["status"] = "executed" if ok else "invalid"

        elif cmd == "set_mic_muted":
            if st is None or not isinstance(data.get("muted"), bool):
                ack["status"] = "invalid"
            else:
                st.set_mic_muted(data["muted"])

        elif cmd == "set_sleep":
            if st is None or not isinstance(data.get("sleeping"), bool):
                ack["status"] = "invalid"
            else:
                st.set_sleeping(data["sleeping"])

        elif cmd == "reload_settings":
            if st is None:
                ack["status"] = "invalid"
            else:
                st.reload_settings()

        elif cmd == "get_status":
            ack["snapshot"] = self.get_snapshot()

        elif cmd == "user_text":
            text = data.get("text")
            now = time.monotonic()
            if not hasattr(websocket, "_user_text_ts"):
                websocket._user_text_ts = collections.deque(maxlen=5)
            ts_queue = websocket._user_text_ts
            if len(ts_queue) == 5 and (now - ts_queue[0]) < 1.0:
                ack["status"] = "rate_limited"
            elif st is None or not isinstance(text, str) or not text.strip() or len(text) > 2000:
                ack["status"] = "invalid"
            else:
                ts_queue.append(now)
                ack["status"] = "executed" if st.submit_text(text) else "unavailable"

        elif cmd == "cancel_countdown":
            from backend.countdown import request_cancel
            request_cancel()

        elif cmd == "shutdown":
            if st is None:
                ack["status"] = "invalid"
            else:
                await websocket.send(json.dumps(ack))
                st.request_shutdown()
                return

        await websocket.send(json.dumps(ack))
