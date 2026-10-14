"""
execution.py
============
Command execution tool for Kira.
Runs allow-listed commands inside workspace/ in a visible CMD window,
capturing output via a temporary file.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

import yaml

from backend import policy as policy_module
from backend.policy import resolve_path

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).parent.parent.parent / "config.yaml"

DEFAULT_ALLOWED_COMMANDS = ["python", "py", "pytest", "dir", "echo", "type"]


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
    """Check if the first token/executable of command_str is allowlisted."""
    tokens = command_str.strip().split()
    if not tokens:
        return False
    binary = tokens[0].lower().replace("/", "\\").split("\\")[-1]
    if binary.endswith(".exe"):
        binary = binary[:-4]
    allowed = _get_allowed_commands(config_path)
    return binary in allowed


def run_command(
    command: str,
    cwd: Optional[str] = None,
    config_path: Path = _DEFAULT_CONFIG,
    visible: bool = True,
) -> str:
    """
    Execute an allow-listed command in a visible CMD window inside workspace/.
    Saves the output to a temporary file, reads it back, and cleans up.

    Parameters
    ----------
    command:
        The command line string to execute (e.g. 'python demo/hello.py').
    cwd:
        Relative directory inside workspace/ to run the command in.
    config_path:
        Path to config.yaml for allow-list lookup.
    visible:
        Whether to open a visible CMD window (Windows only).
    """
    if not is_command_allowlisted(command, config_path):
        return f"Error: Command '{command}' is not in the allow-list. Execution rejected."

    # Determine execution directory inside workspace/
    if cwd:
        working_dir = resolve_path(cwd)
    else:
        working_dir = policy_module.WORKSPACE_ROOT

    # Temporary file for capturing output
    tmp_id = uuid.uuid4().hex[:8]
    output_file = (policy_module.WORKSPACE_ROOT / f".tmp_cmd_{tmp_id}.txt").resolve()

    try:
        is_interactive = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
        if os.name == "nt" and visible and is_interactive:
            # Launch in visible CMD window on Windows using start /wait
            # Wrap command to pipe both stdout and stderr into output_file
            cmd_line = f'start /wait "" cmd.exe /c "{command} > "{output_file}" 2>&1"'
            subprocess.run(
                ["cmd.exe", "/c", cmd_line],
                cwd=str(working_dir),
                check=False,
            )
        else:
            # Fallback for non-Windows, headless mode, or tests: run directly and write to file
            with open(output_file, "w", encoding="utf-8", errors="replace") as out:
                subprocess.run(
                    command,
                    shell=True,
                    cwd=str(working_dir),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    check=False,
                )

        if output_file.exists():
            output_text = output_file.read_text(encoding="utf-8", errors="replace").strip()
            return output_text if output_text else "(Command finished with no output)"
        return "(Command finished, no output file generated)"

    except Exception as exc:
        return f"Command execution failed: {exc}"

    finally:
        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass
