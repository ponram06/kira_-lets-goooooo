"""
execution.py
============
Command execution tool for Kira.
Safely parses command strings, blocks shell metacharacters, validates path
arguments against workspace boundaries, and executes without shell=True.
Output is displayed in a visible CMD window (Windows) and saved to a
code-generated temporary file with active 30s timeout and kill switch monitoring.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import yaml

from backend import policy as policy_module
from backend.policy import KILL_SWITCH, PolicyError, resolve_path

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).parent.parent.parent / "config.yaml"

DEFAULT_ALLOWED_COMMANDS = ["python", "py", "pytest", "dir", "echo", "type"]
SHELL_METACHARS = frozenset(["&", "|", ">", "<", "^", "%", "\n", "\r", ";"])


def has_shell_metachars(command: str) -> bool:
    """Check if command contains any shell chaining/redirection metacharacters."""
    return any(c in command for c in SHELL_METACHARS)


def is_cli_flag(arg: str) -> bool:
    """Return True if arg is a command-line switch/option rather than a path."""
    if arg.startswith("-"):
        return True
    if arg.startswith("/") and arg.count("/") == 1 and (arg[1:].isalnum() or arg[1:] in ("?",)):
        return True
    return False


def _terminate_process(proc: subprocess.Popen) -> None:
    """Safely terminate a process and its children."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def _get_allowed_commands(config_path: Path = _DEFAULT_CONFIG) -> list[str]:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
            return [
                c.lower()
                for c in cfg.get("tools", {}).get("allowed_commands", DEFAULT_ALLOWED_COMMANDS)
            ]
    except Exception:
        return DEFAULT_ALLOWED_COMMANDS


def is_command_allowlisted(command_str: str, config_path: Path = _DEFAULT_CONFIG) -> bool:
    """Check if the first binary token of command_str is allowlisted."""
    try:
        tokens = shlex.split(command_str.strip(), posix=False)
    except Exception:
        tokens = command_str.strip().split()

    if not tokens:
        return False

    binary = tokens[0].lower().replace("/", "\\").split("\\")[-1]
    if binary.endswith(".exe"):
        binary = binary[:-4]

    allowed = _get_allowed_commands(config_path)
    return binary in allowed


def inspect_command(
    command: str,
    config_path: Path = _DEFAULT_CONFIG,
) -> tuple[bool, bool, list[str]]:
    """
    Inspect command string for policy checking.

    Returns
    -------
    ``(is_chained, is_allowlisted, tokens)``
    """
    if has_shell_metachars(command):
        return True, False, []

    try:
        tokens = shlex.split(command.strip(), posix=False)
    except Exception:
        tokens = command.strip().split()

    if not tokens:
        return False, False, []

    allowlisted = is_command_allowlisted(command, config_path)
    return False, allowlisted, tokens


def run_command(
    command: str,
    cwd: Optional[str] = None,
    config_path: Path = _DEFAULT_CONFIG,
    visible: bool = True,
    timeout_seconds: float = 30.0,
) -> str:
    """
    Execute a command inside workspace/ without shell=True.

    Guards
    ------
    - Kill switch check before, during, and after execution.
    - Shell metacharacters are rejected.
    - All path arguments are verified via resolve_path().
    - 30-second execution timeout.
    - Output is written to a code-generated temporary file in workspace/.
    """
    # 1. Kill switch check
    if KILL_SWITCH.is_stopped():
        return "Execution halted: Kill switch is active."

    # 2. Block shell metacharacters / chaining
    if has_shell_metachars(command):
        return f"Error: Shell metacharacters or command chaining detected in '{command}'. Blocked."

    # 3. Parse command arguments safely
    try:
        tokens = shlex.split(command.strip(), posix=False)
    except Exception as exc:
        return f"Error: Failed to parse command: {exc}"

    if not tokens:
        return "Error: Empty command."

    binary = tokens[0]
    args = tokens[1:]

    # 4. Resolve path arguments to verify they stay inside workspace/
    for arg in args:
        if is_cli_flag(arg):
            continue
        # Check if arg looks like a file/path argument
        is_path_like = (
            "/" in arg
            or "\\" in arg
            or any(arg.lower().endswith(ext) for ext in [".py", ".txt", ".json", ".md", ".yaml", ".yml", ".csv", ".bat", ".cmd", ".sh"])
            or (policy_module.WORKSPACE_ROOT / arg).exists()
        )
        if is_path_like:
            try:
                resolve_path(arg)
            except PolicyError as pe:
                return f"Error: Path argument '{arg}' escapes the workspace: {pe}"

    # Determine execution directory inside workspace/
    if cwd:
        try:
            working_dir = resolve_path(cwd)
        except PolicyError as pe:
            return f"Error: Invalid working directory: {pe}"
    else:
        working_dir = policy_module.WORKSPACE_ROOT

    # 5. Code-generated temporary output file inside workspace/
    tmp_id = uuid.uuid4().hex
    output_file = (policy_module.WORKSPACE_ROOT / f".tmp_cmd_{tmp_id}.txt").resolve()

    proc: Optional[subprocess.Popen] = None
    try:
        is_interactive = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

        if os.name == "nt" and visible and is_interactive:
            # Launch in visible CMD window on Windows:
            # displays output in window AND saves to temp file, keeps open ~3s
            cmd_line = f'start /wait "" cmd.exe /c "{command} > "{output_file}" 2>&1 & type "{output_file}" & timeout /t 3 > nul"'
            proc = subprocess.Popen(
                ["cmd.exe", "/c", cmd_line],
                cwd=str(working_dir),
                shell=False,
            )
        else:
            # Direct execution without shell
            with open(output_file, "w", encoding="utf-8", errors="replace") as out_f:
                proc = subprocess.Popen(
                    tokens,
                    cwd=str(working_dir),
                    stdout=out_f,
                    stderr=subprocess.STDOUT,
                    shell=False,
                )

        # 6. Monitor process with 30s timeout and kill switch checks
        start_time = time.monotonic()
        while proc.poll() is None:
            if KILL_SWITCH.is_stopped():
                _terminate_process(proc)
                return "Execution halted: Kill switch was activated during command execution."

            if time.monotonic() - start_time > timeout_seconds:
                _terminate_process(proc)
                return f"Error: Command timed out after {timeout_seconds} seconds."

            time.sleep(0.05)

        # Final post-execution kill switch check
        if KILL_SWITCH.is_stopped():
            return "Execution halted: Kill switch was activated."

        if output_file.exists():
            content = output_file.read_text(encoding="utf-8", errors="replace").strip()
            return content if content else "(Command executed successfully with no output)"
        return "(Command completed, no output file produced)"

    except Exception as exc:
        return f"Error: Command execution failed: {exc}"

    finally:
        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass
