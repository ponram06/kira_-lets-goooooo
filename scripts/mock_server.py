"""
scripts/mock_server.py
======================
Mock Kira WebSocket Server for testing the Electron Avatar frontend.
Simulates backend event streams (idle, listening, thinking, working, speaking with loudness, error)
and validates incoming token auth and kill switch stop commands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import secrets
import signal
import sys
from pathlib import Path

try:
    import websockets
    from websockets.server import serve
except ImportError:
    print("Error: 'websockets' package is required. Run in .venv with dependencies installed.")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("mock_server")

ROOT_DIR = Path(__file__).resolve().parent.parent
TOKEN_DIR = ROOT_DIR / ".kira"
TOKEN_FILE = TOKEN_DIR / "token"
HOST = "127.0.0.1"
PORT = 8765


def setup_token() -> str:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    logger.info("Mock auth token generated and written to: %s", TOKEN_FILE)
    return token


def cleanup_token() -> None:
    try:
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
            logger.info("Cleaned up mock token file.")
    except Exception as err:
        logger.warning("Could not delete token file: %s", err)


async def avatar_handler(websocket, expected_token: str):
    logger.info("Avatar connection initiated from %s", websocket.remote_address)

    # 1. First message must be the auth token
    try:
        first_msg = await asyncio.wait_for(websocket.recv(), timeout=5.0)
        token_str = str(first_msg).strip()
        if not secrets.compare_digest(token_str, expected_token):
            logger.warning("Unauthorized connection rejected (invalid token).")
            await websocket.close(1008, "Invalid token")
            return
        logger.info("Avatar client authenticated successfully.")
    except asyncio.TimeoutError:
        logger.warning("Connection timed out waiting for token.")
        await websocket.close(1008, "Auth timeout")
        return
    except Exception as err:
        logger.error("Error during authentication handshake: %s", err)
        return

    # 2. Start state simulation and command listener
    stop_event = asyncio.Event()

    async def incoming_listener():
        try:
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                    cmd = data.get("command")
                    logger.info("Received command from avatar: %s", cmd)
                    if cmd == "stop":
                        logger.warning(">>> STOP COMMAND RECEIVED! KILL SWITCH TRIGGERED <<<")
                        await websocket.send(json.dumps({
                            "type": "speech",
                            "text": "Emergency stop activated. All tasks halted."
                        }))
                        await websocket.send(json.dumps({
                            "type": "state",
                            "state": "error"
                        }))
                except Exception as e:
                    logger.warning("Failed to parse incoming command: %s", e)
        except Exception:
            pass
        finally:
            stop_event.set()

    listener_task = asyncio.create_task(incoming_listener())

    # Simulated lifecycle cycle
    states = [
        ("idle", "Ready for commands.", 4.0),
        ("listening", "Listening...", 3.0),
        ("thinking", "Analyzing code structure...", 3.0),
        ("working", "Refactoring files in workspace...", 4.0),
        ("speaking", "I found 3 optimizations in the architecture. Let me explain each one.", 5.0)
    ]

    try:
        # Send initial mode event
        await websocket.send(json.dumps({"type": "mode", "mode": "restricted"}))

        while not stop_event.is_set():
            for state_name, speech_text, duration in states:
                if stop_event.is_set():
                    break

                logger.info("Broadcasting state: %s", state_name)
                await websocket.send(json.dumps({"type": "state", "state": state_name}))

                if speech_text:
                    await websocket.send(json.dumps({"type": "speech", "text": speech_text}))

                if state_name == "speaking":
                    # Stream simulated loudness for 5 seconds at 20Hz
                    steps = int(duration * 20)
                    for step in range(steps):
                        if stop_event.is_set():
                            break
                        loudness = 0.2 + 0.6 * abs(math.sin(step * 0.4))
                        await websocket.send(json.dumps({"type": "loudness", "value": round(loudness, 2)}))
                        await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(duration)

    except websockets.exceptions.ConnectionClosed:
        logger.info("Avatar disconnected.")
    finally:
        listener_task.cancel()


async def main():
    token = setup_token()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: sys.exit(0))
        except NotImplementedError:
            pass

    try:
        async with serve(lambda ws: avatar_handler(ws, token), HOST, PORT):
            logger.info("Mock WebSocket Server running at ws://%s:%d", HOST, PORT)
            logger.info("Press Ctrl+C to stop.")
            await asyncio.Future()  # run forever
    finally:
        cleanup_token()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        cleanup_token()
        logger.info("Mock server shut down.")
