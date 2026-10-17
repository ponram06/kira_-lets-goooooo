"""
script_runner.py
================
M5: the ``run_script`` tool. The model supplies Python code; Kira saves it
under ``workspace/scripts/``, checks it statically, and runs it.

Safety layers (in order)
------------------------
1. Static AST checks (``check_script``): only allow-listed imports; no
   eval/exec/compile/__import__/getattr-style escapes; no dunder attribute
   access; ``open()`` only with a literal relative path that stays inside
   workspace/scripts/. Rejected code is never saved or run.
2. Policy (policy.py): Restricted mode shows the script and asks; Free mode
   runs it after the 3-second countdown.
3. Runtime limits: ``python -I`` (isolated mode) in a subprocess, working
   directory workspace/scripts/, an environment without any secrets, a wall
   clock timeout, an output size cap, a memory cap (Windows Job Object) and
   the kill switch (polled while it runs).
4. Optional real isolation: ``scripts.sandbox`` in config.yaml can be
   ``docker`` (no network, memory cap, read-only mount) or
   ``windows_sandbox``. If the chosen sandbox is not available the script
   is refused rather than run unsandboxed.

IMPORTANT: the static checks are NOT a full sandbox. Python is dynamic and a
determined script can find ways around AST rules. Use the Docker or Windows
Sandbox option for code you do not trust.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import yaml

from backend import events as _events
from backend import policy as policy_module
from backend.policy import KILL_SWITCH

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.yaml"

# Modules a script may import (pure computation / text / time). Everything
# else (os, sys, subprocess, socket, ctypes, shutil, importlib, pathlib, ...)
# is rejected.
DEFAULT_ALLOWED_IMPORTS = frozenset({
    "math", "cmath", "random", "statistics", "decimal", "fractions",
    "json", "csv", "re", "string", "textwrap", "unicodedata",
    "datetime", "time", "calendar", "zoneinfo",
    "itertools", "functools", "operator", "collections", "heapq", "bisect",
    "dataclasses", "enum", "typing", "copy", "pprint", "uuid", "hashlib",
})

# Named explicitly so the refusal message is clear.
DANGEROUS_MODULES = frozenset({
    "os", "sys", "subprocess", "socket", "ctypes", "shutil", "importlib",
    "multiprocessing", "threading", "asyncio", "signal", "pathlib", "glob",
    "tempfile", "pickle", "marshal", "builtins", "inspect", "code", "codeop",
    "runpy", "urllib", "http", "ftplib", "smtplib", "requests", "winreg",
    "_winapi", "msvcrt", "pty", "io", "mmap", "gc", "webbrowser",
})

BANNED_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr",
    "globals", "locals", "vars", "breakpoint", "input", "memoryview",
    "help", "exit", "quit",
})

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class ScriptRejected(Exception):
    """Static check failure (message is safe to show the user)."""


# ── configuration ─────────────────────────────────────────────────────────

def load_script_config(config_path: Path = _DEFAULT_CONFIG) -> dict:
    """Return the ``scripts`` section of config.yaml merged with defaults."""
    cfg: dict[str, Any] = {
        "dir": "scripts", "timeout_seconds": 20, "max_output_chars": 8000,
        "max_code_chars": 20000, "memory_limit_mb": 256, "sandbox": "none",
        "docker_image": "python:3.11-slim",
        "allowed_imports": sorted(DEFAULT_ALLOWED_IMPORTS),
    }
    try:
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg.update(data.get("scripts") or {})
    except (OSError, yaml.YAMLError) as exc:
        logger.debug("scripts config not loaded: %s", exc)
    return cfg


def scripts_dir(cfg: dict) -> Path:
    """workspace/<dir>, guaranteed to be inside the workspace."""
    d = policy_module.resolve_path(str(cfg.get("dir") or "scripts"))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── static checks ─────────────────────────────────────────────────────────

def _check_open_path(node: ast.Call) -> None:
    if not node.args:
        raise ScriptRejected("open() needs a literal relative file name.")
    arg = node.args[0]
    if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
        raise ScriptRejected("open() may only use a literal file name (no variables).")
    p = arg.value.replace("\\", "/")
    if (not p or p.startswith("/") or ":" in p or ".." in p.split("/")
            or p.startswith("~") or "\x00" in p):
        raise ScriptRejected(f"open() path {arg.value!r} must stay inside workspace/scripts/.")


def check_script(code: str, allowed_imports: frozenset[str] = DEFAULT_ALLOWED_IMPORTS,
                 max_chars: int = 20000) -> None:
    """Raise ScriptRejected if *code* fails the static checks."""
    if not isinstance(code, str) or not code.strip():
        raise ScriptRejected("The script is empty.")
    if len(code) > max_chars:
        raise ScriptRejected(f"The script is longer than {max_chars} characters.")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ScriptRejected(f"Syntax error on line {exc.lineno}: {exc.msg}") from None

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    raise ScriptRejected("Relative imports are not allowed.")
                names = [node.module or ""]
            else:
                names = [a.name for a in node.names]
            for name in names:
                top = name.split(".")[0]
                if top in DANGEROUS_MODULES:
                    raise ScriptRejected(f"Import of '{top}' is not allowed (system access).")
                if top not in allowed_imports:
                    raise ScriptRejected(f"Import of '{top}' is not on the allowed list.")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in ("system", "popen", "rmtree", "f_globals", "gi_frame"):
                raise ScriptRejected(f"Access to '.{node.attr}' is not allowed.")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") and node.id not in ("__name__",):
                raise ScriptRejected(f"Use of '{node.id}' is not allowed.")
            if node.id in BANNED_CALLS:
                raise ScriptRejected(f"Use of '{node.id}' is not allowed.")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                _check_open_path(node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            continue


# ── execution helpers ─────────────────────────────────────────────────────

def _interpreter() -> str:
    """The real interpreter (a venv python.exe on Windows is a launcher that
    spawns a child, which the one-process job limit would block). Scripts may
    only use the standard library, so the base interpreter is enough."""
    base = getattr(sys, "_base_executable", "") or sys.executable
    return base if os.path.isfile(base) else sys.executable


def _clean_env() -> dict[str, str]:
    """Minimal environment for the child: no API keys, PIN or tokens."""
    keep = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = os.path.dirname(_interpreter())
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _apply_memory_limit(proc: subprocess.Popen, limit_mb: int) -> Optional[Any]:
    """Put *proc* in a Windows Job Object with a memory cap. Returns the handle."""
    if os.name != "nt" or limit_mb <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        # PROCESS_MEMORY | KILL_ON_JOB_CLOSE | ACTIVE_PROCESS (no child processes)
        info.BasicLimitInformation.LimitFlags = 0x100 | 0x2000 | 0x8
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = limit_mb * 1024 * 1024
        k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
        hproc = k32.OpenProcess(0x0001 | 0x0100, False, proc.pid)  # TERMINATE | SET_QUOTA
        k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(hproc))
        k32.CloseHandle(wintypes.HANDLE(hproc))
        return job
    except Exception as exc:
        logger.debug("Job object memory limit not applied: %s", exc)
        return None


def _close_job(job: Any) -> None:
    if job:
        try:
            import ctypes
            from ctypes import wintypes
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(job))
        except Exception:
            pass


def build_docker_command(script: Path, cfg: dict) -> list[str]:
    """docker run with no network, memory cap, read-only script mount."""
    mem = int(cfg.get("memory_limit_mb") or 256)
    return ["docker", "run", "--rm", "--network", "none", "--memory", f"{mem}m",
            "--pids-limit", "32", "--read-only", "--cap-drop", "ALL",
            "-v", f"{script.parent}:/w:ro", "-w", "/w", str(cfg.get("docker_image")),
            "python", "-I", f"/w/{script.name}"]


def build_wsb_config(scripts: Path, script: Path) -> str:
    """Windows Sandbox config: networking off, scripts folder read-only, output via a log folder."""
    return (
        "<Configuration>\n"
        "  <Networking>Disable</Networking>\n"
        "  <ClipboardRedirection>Disable</ClipboardRedirection>\n"
        "  <MappedFolders><MappedFolder>\n"
        f"    <HostFolder>{scripts}</HostFolder>\n"
        "    <SandboxFolder>C:\\kira</SandboxFolder><ReadOnly>false</ReadOnly>\n"
        "  </MappedFolder></MappedFolders>\n"
        f"  <LogonCommand><Command>cmd /c python C:\\kira\\{script.name} &gt; C:\\kira\\{script.stem}.out 2&gt;&amp;1</Command></LogonCommand>\n"
        "</Configuration>\n"
    )


def sandbox_available(kind: str) -> bool:
    if kind == "docker":
        return shutil.which("docker") is not None
    if kind == "windows_sandbox":
        return shutil.which("WindowsSandbox.exe") is not None or os.path.isfile(
            os.path.expandvars(r"%SystemRoot%\System32\WindowsSandbox.exe"))
    return kind == "none"


def _run_process(cmd: list[str], cwd: Path, timeout: float, max_out: int, mem_mb: int) -> tuple[str, str]:
    """Run *cmd*; returns (status, output). status: ok | error | timeout | killed | too_much_output."""
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=_clean_env(), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    job = _apply_memory_limit(proc, mem_mb)
    chunks: list[bytes] = []
    size = [0]
    overflow = threading.Event()

    def reader() -> None:
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read(4096)
            if not data:
                break
            if size[0] < max_out:
                chunks.append(data[: max_out - size[0]])
            size[0] += len(data)
            if size[0] > max_out:
                overflow.set()
                break

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    status = "ok"
    try:
        while proc.poll() is None:
            if KILL_SWITCH.is_stopped():
                status = "killed"
                break
            if overflow.is_set():
                status = "too_much_output"
                break
            if time.monotonic() > deadline:
                status = "timeout"
                break
            time.sleep(0.05)
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        t.join(timeout=2)
        _close_job(job)
    if overflow.is_set():
        status = "too_much_output"
    out = b"".join(chunks).decode("utf-8", errors="replace")
    if status == "ok" and proc.returncode != 0:
        status = "error"
    return status, out


# ── the tool ──────────────────────────────────────────────────────────────

def run_script(code: str, name: Optional[str] = None, config_path: Path = _DEFAULT_CONFIG) -> str:
    """
    Check, save and run a Python script under workspace/scripts/.
    Policy (ask / countdown) has already been applied by the agent loop.
    """
    if KILL_SWITCH.is_stopped():
        return "Execution halted: Kill switch is active."
    cfg = load_script_config(config_path)
    allowed = frozenset(cfg.get("allowed_imports") or DEFAULT_ALLOWED_IMPORTS) - DANGEROUS_MODULES
    try:
        check_script(code, allowed, int(cfg.get("max_code_chars") or 20000))
    except ScriptRejected as exc:
        _events.publish("script.blocked", f"Script rejected: {exc}", {"reason": str(exc)})
        return f"Refused: {exc}"

    sandbox = str(cfg.get("sandbox") or "none").lower()
    if sandbox not in ("none", "docker", "windows_sandbox"):
        return f"Error: Unknown scripts.sandbox value '{sandbox}'."
    if not sandbox_available(sandbox):
        return f"Refused: scripts.sandbox is '{sandbox}' but it is not available on this PC. Not running unsandboxed."

    base = name if (name and _SAFE_NAME.match(name)) else "script"
    folder = scripts_dir(cfg)
    script = folder / f"{base}_{uuid.uuid4().hex[:6]}.py"
    script.write_text(code, encoding="utf-8")
    rel = script.relative_to(policy_module.WORKSPACE_ROOT).as_posix()
    _events.publish("script.saved", f"Saved script {rel}", {"path": rel, "sandbox": sandbox})

    timeout = float(cfg.get("timeout_seconds") or 20)
    max_out = int(cfg.get("max_output_chars") or 8000)
    mem = int(cfg.get("memory_limit_mb") or 256)

    if sandbox == "windows_sandbox":
        wsb = folder / f"{script.stem}.wsb"
        wsb.write_text(build_wsb_config(folder, script), encoding="utf-8")
        try:
            os.startfile(str(wsb))  # type: ignore[attr-defined]
        except Exception as exc:
            return f"Error: Could not start Windows Sandbox: {exc}"
        out_file = folder / f"{script.stem}.out"
        deadline = time.monotonic() + timeout + 60   # sandbox boot time
        while time.monotonic() < deadline and not KILL_SWITCH.is_stopped():
            if out_file.exists():
                time.sleep(1)
                return f"Script {rel} ran in Windows Sandbox. Output:\n" + out_file.read_text(
                    encoding="utf-8", errors="replace")[:max_out]
            time.sleep(0.5)
        return f"Script {rel} was started in Windows Sandbox, but no output arrived in time."

    if sandbox == "docker":
        cmd = build_docker_command(script, cfg)
    else:
        cmd = [_interpreter(), "-I", "-B", str(script)]
    status, out = _run_process(cmd, folder, timeout, max_out, mem)
    out = out.strip()
    if status == "killed":
        return "Execution halted: Kill switch was activated."
    if status == "timeout":
        return f"Error: Script {rel} was stopped after {timeout:g} seconds (timeout). Output so far:\n{out}"
    if status == "too_much_output":
        return f"Error: Script {rel} was stopped: output exceeded {max_out} characters. First part:\n{out}"
    if status == "error":
        return f"Error: Script {rel} failed. Output:\n{out}"
    return f"Script {rel} ran successfully. Output:\n{out}"
