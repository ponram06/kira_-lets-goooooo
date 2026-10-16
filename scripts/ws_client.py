"""
ws_client.py
============
Interactive CLI test client for the Kira WebSocket event stream.

Connects to the local Kira WebSocket server, authenticates using the
ephemeral token from .kira/token, and displays events in real-time.
Also allows sending commands (stop, confirm yes/no, restricted) from stdin.

Usage
-----
In a second terminal while Kira is running::

    python scripts/ws_client.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import threading
import time

import yaml
from websockets.asyncio.client import connect

_ROOT_DIR = Path(__file__).parent.parent
_DEFAULT_CONFIG = _ROOT_DIR / "config.yaml"


def load_server_config() -> tuple[str, int, Path]:
    host = "127.0.0.1"
    port = 8765
    token_path = _ROOT_DIR / ".kira" / "token"

    if _DEFAULT_CONFIG.exists():
        try:
            with open(_DEFAULT_CONFIG, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
                sc = cfg.get("server", {})
                host = sc.get("host", host)
                port = int(sc.get("port", port))
                tf = sc.get("token_file", ".kira/token")
                p = Path(tf)
                token_path = p if p.is_absolute() else (_ROOT_DIR / p).resolve()
        except Exception:
            pass

    return host, port, token_path


def format_loudness_bar(level: float, width: int = 15) -> str:
    filled = int(round(level * width))
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {level:.2f}"


async def client_main() -> None:
    host, port, token_file = load_server_config()

    if not token_file.exists():
        print("=" * 60)
        print(f"  [Error] Token file not found at: {token_file}")
        print("  Make sure Kira is running in another terminal:")
        print("      python -m backend.brain")
        print("=" * 60)
        return

    token = token_file.read_text(encoding="utf-8").strip()
    uri = f"ws://{host}:{port}"

    print("=" * 60)
    print(f"  Connecting to Kira WebSocket: {uri}")
    print(f"  Token file: {token_file}")
    print("  Commands you can type: 'stop', 'yes [qid]', 'no [qid]', 'restricted'")
    print("=" * 60)

    try:
        async with connect(uri, additional_headers={"Origin": "null"}) as ws:
            # 1. Send authentication frame
            auth_payload = json.dumps({"type": "auth", "token": token})
            await ws.send(auth_payload)

            # 2. Wait for auth confirmation
            auth_resp_raw = await ws.recv()
            auth_resp = json.loads(auth_resp_raw)
            if auth_resp.get("status") != "authenticated" and auth_resp.get("type") != "auth_ok":
                print(f"  [Auth Failed]: {auth_resp_raw}")
                return

            print("  [Connected & Authenticated Successfully!]\n")

            active_question_id: str = ""

            # Background task to send stdin user input to the server
            cmd_queue: asyncio.Queue[str] = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def stdin_listener():
                while True:
                    try:
                        line = sys.stdin.readline()
                        if not line:
                            break
                        cmd = line.strip()
                        if cmd:
                            loop.call_soon_threadsafe(cmd_queue.put_nowait, cmd)
                    except Exception:
                        break

            t = threading.Thread(target=stdin_listener, daemon=True)
            t.start()

            async def command_sender():
                nonlocal active_question_id
                while True:
                    cmd_line = await cmd_queue.get()
                    parts = cmd_line.split()
                    verb = parts[0].lower()

                    if verb == "stop":
                        await ws.send(json.dumps({"command": "stop"}))
                        print("  >> Sent: stop")
                    elif verb in ("yes", "y", "no", "n"):
                        val = "yes" if verb.startswith("y") else "no"
                        qid = parts[1] if len(parts) > 1 else active_question_id
                        if not qid:
                            print("  [!] Please specify question_id: 'yes <qid>'")
                        else:
                            await ws.send(json.dumps({"command": "confirm", "question_id": qid, "value": val}))
                            print(f"  >> Sent: confirm {val} for question {qid}")
                    elif verb in ("restricted", "restricted_mode"):
                        await ws.send(json.dumps({"command": "restricted_mode"}))
                        print("  >> Sent: restricted_mode")
                    elif verb in ("free", "free_mode"):
                        await ws.send(json.dumps({"command": "free_mode"}))
                        print("  >> Sent: free_mode")
                    else:
                        print(f"  [!] Unknown command: '{cmd_line}'")

            sender_task = asyncio.create_task(command_sender())

            # Event reader loop
            try:
                async for raw in ws:
                    try:
                        ev = json.loads(raw)
                    except Exception:
                        print(f"  [RAW] {raw}")
                        continue

                    etype = ev.get("type")
                    ts = ev.get("timestamp", "")
                    time_str = ts.split("T")[-1][:8] if "T" in ts else ts

                    if etype == "snapshot":
                        print(f"[{time_str}] [SNAPSHOT] State: {ev.get('state', '').upper()} | Mode: {ev.get('mode', '').upper()}")
                    elif etype == "state":
                        print(f"[{time_str}] [STATE] -> {ev.get('state', '').upper()}")
                    elif etype == "step_start":
                        print(f"[{time_str}] [STEP START] #{ev.get('step')} Tool: {ev.get('tool')}")
                    elif etype == "step_done":
                        print(f"[{time_str}] [STEP DONE] #{ev.get('step')} Tool: {ev.get('tool')} | {ev.get('summary')}")
                    elif etype == "policy_ask":
                        active_question_id = ev.get("question_id", "")
                        print(f"\n[{time_str}] [ACTION CONFIRMATION REQUIRED]")
                        print(f"  Question ID: {active_question_id}")
                        print(f"  Prompt:      {ev.get('prompt')}")
                        print(f"  Respond by typing: 'yes {active_question_id}' or 'no {active_question_id}'\n")
                    elif etype == "mode_change":
                        print(f"[{time_str}] [MODE CHANGE] -> {ev.get('mode', '').upper()}")
                    elif etype == "speech":
                        print(f"[{time_str}] [SPEECH] {ev.get('text')}")
                    elif etype == "loudness":
                        bar = format_loudness_bar(ev.get("level", 0.0))
                        sys.stdout.write(f"\r[{time_str}] [LOUDNESS] {bar}  ")
                        sys.stdout.flush()
                    elif etype == "command_ack":
                        print(f"\n[{time_str}] [ACK] Command: {ev.get('command')} -> Status: {ev.get('status')}")
                    elif etype == "error":
                        print(f"\n[{time_str}] [SERVER ERROR] {ev.get('message')}")
                    else:
                        print(f"[{time_str}] [{etype}] {ev}")

            finally:
                sender_task.cancel()

    except ConnectionRefusedError:
        print(f"  [Error] Connection refused to {uri}. Is Kira running?")
    except KeyboardInterrupt:
        print("\n  [Exiting ws_client...]")


def main() -> None:
    try:
        asyncio.run(client_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
