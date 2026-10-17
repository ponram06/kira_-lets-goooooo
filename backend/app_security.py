"""
app_security.py
===============
Security validation, shortcut resolution, and denylist enforcement for Kira's
universal app launcher.

Enforces:
1. .lnk resolution extracting target path, arguments, and SLDF_RUNAS_USER flag.
2. Refusal of scripts and script interpreters (.bat, .cmd, .ps1, mshta, etc.).
3. Whole-word denylist matching on target executable names and Store package names.
4. Display-name token matching ("Windows PowerShell", "Command Prompt", "Windows Terminal").
5. Refusal of administrator elevation (UAC prompt avoidance).
"""

from __future__ import annotations

import logging
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Extensions that execute as scripts or system consoles
REFUSED_EXTENSIONS = frozenset(
    [
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".msc",
        ".hta",
        ".reg",
    ]
)

# Interpreters and execution hosts that must never be launched as apps
REFUSED_INTERPRETERS = frozenset(
    [
        "mshta.exe",
        "wscript.exe",
        "cscript.exe",
        "rundll32.exe",
        "regsvr32.exe",
        "certutil.exe",
        "bitsadmin.exe",
    ]
)

# Whole-word denylist stems (exact stem without .exe)
DENIED_EXECUTABLE_STEMS = frozenset(
    [
        "cmd",
        "powershell",
        "pwsh",
        "wt",
        "bash",
        "wsl",
        "wslhost",
        "python",
        "pythonw",
        "py",
        "regedit",
        "regedt32",
        "mmc",
        "diskpart",
        "diskmgmt",
        "format",
        "fdisk",
        "cleanmgr",
        "secpol",
        "gpedit",
        "lusrmgr",
        "services",
        "eventvwr",
        "taskmgr",
    ]
)

# Installer / uninstaller stem patterns (whole-word or exact prefix)
_INSTALLER_PATTERN = re.compile(
    r"\b(unins\w*|uninstall\w*|setup\w*|installer\w*|update\w*)\b",
    re.IGNORECASE,
)

# Prohibited display name patterns (whole word / phrase)
_BLOCKED_DISPLAY_NAMES = [
    re.compile(r"\bwindows\s+powershell\b", re.IGNORECASE),
    re.compile(r"\bcommand\s+prompt\b", re.IGNORECASE),
    re.compile(r"\bwindows\s+terminal\b", re.IGNORECASE),
    re.compile(r"\bregistry\s+editor\b", re.IGNORECASE),
    re.compile(r"\bdisk\s+management\b", re.IGNORECASE),
    re.compile(r"\bcomputer\s+management\b", re.IGNORECASE),
    re.compile(r"\bevent\s+viewer\b", re.IGNORECASE),
    re.compile(r"\btask\s+manager\b", re.IGNORECASE),
    re.compile(r"\bdevice\s+manager\b", re.IGNORECASE),
    re.compile(r"\bgroup\s+policy\b", re.IGNORECASE),
    re.compile(r"\blocalsecurity\s+policy\b", re.IGNORECASE),
]

# Denied Store App packages (whole word matching)
_DENIED_STORE_PACKAGES = frozenset(
    [
        "microsoft.windowsterminal",
        "microsoft.powershell",
    ]
)

# Windows Shell Link flag for SLDF_RUNAS_USER
SLDF_RUNAS_USER = 0x00002000


@dataclass(frozen=True)
class ShortcutInfo:
    target_path: str
    arguments: str
    run_as_admin: bool
    is_valid: bool
    error: Optional[str] = None


def _parse_lnk_binary_fallback(lnk_path: Path) -> tuple[str, str, bool]:
    """Fallback binary parser for .lnk header flags (offset 0x14)."""
    try:
        data = lnk_path.read_bytes()
        if len(data) >= 76 and data[:4] == b"\x4c\x00\x00\x00":
            flags = struct.unpack("<I", data[0x14:0x18])[0]
            run_as = bool(flags & SLDF_RUNAS_USER)
            return "", "", run_as
    except Exception:
        pass
    return "", "", False


def resolve_shortcut(lnk_path: Path | str) -> ShortcutInfo:
    """
    Resolve a Windows .lnk shortcut file.
    Extracts target path, arguments, and the SLDF_RUNAS_USER flag.
    Uses IShellLink COM interface with a robust fallback.
    """
    path = Path(lnk_path).resolve()
    if not path.is_file():
        return ShortcutInfo("", "", False, False, f"Shortcut not found: {path}")

    # Primary method: COM IShellLink + IShellLinkDataList
    try:
        import pythoncom
        from win32com.shell import shell, shellcon

        pythoncom.CoInitialize()
        try:
            link = pythoncom.CoCreateInstance(
                shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink
            )
            persist_file = link.QueryInterface(pythoncom.IID_IPersistFile)
            persist_file.Load(str(path))

            target, _ = link.GetPath(shell.SLGP_UNCPRIORITY)
            args = link.GetArguments() or ""

            # Check RunAs flag via IShellLinkDataList
            run_as = False
            try:
                data_list = link.QueryInterface(shell.IID_IShellLinkDataList)
                flags = data_list.GetFlags()
                run_as = bool(flags & getattr(shellcon, "SLDF_RUNAS_USER", SLDF_RUNAS_USER))
            except Exception:
                _, _, run_as = _parse_lnk_binary_fallback(path)

            return ShortcutInfo(
                target_path=target.strip(),
                arguments=args.strip(),
                run_as_admin=run_as,
                is_valid=True,
            )
        finally:
            pythoncom.CoUninitialize()
    except Exception as exc:
        logger.debug("COM shortcut resolution failed for %s (%s); trying WScript fallback", path, exc)

    # Secondary method: WScript.Shell
    try:
        import win32com.client
        wscript = win32com.client.Dispatch("WScript.Shell")
        sc = wscript.CreateShortcut(str(path))
        target = sc.TargetPath or ""
        args = sc.Arguments or ""
        _, _, run_as = _parse_lnk_binary_fallback(path)
        return ShortcutInfo(
            target_path=target.strip(),
            arguments=args.strip(),
            run_as_admin=run_as,
            is_valid=True,
        )
    except Exception as exc:
        return ShortcutInfo("", "", False, False, f"Failed resolving shortcut: {exc}")


def check_script_or_interpreter(target_path: str) -> tuple[bool, str]:
    """
    Check if the target is a script (.bat, .cmd, .ps1, etc.) or an execution host.
    Returns (is_refused, reason).
    """
    if not target_path:
        return False, ""

    p = Path(target_path)
    ext = p.suffix.lower()
    if ext in REFUSED_EXTENSIONS:
        return True, f"Target '{p.name}' is a script ({ext}) and cannot be launched directly."

    name = p.name.lower()
    if name in REFUSED_INTERPRETERS:
        return True, f"Target '{p.name}' is a script interpreter/host and is blocked by policy."

    return False, ""


def check_denylist(
    target_path: str = "",
    display_name: str = "",
    arguments: str = "",
    is_store_app: bool = False,
    app_id: str = "",
) -> tuple[bool, str]:
    """
    Check application against security denylist using whole-word matching.
    Blocks installers, command shells, registry tools, partition editors, etc.
    Returns (is_blocked, reason).
    """
    # 1. Check display name patterns (e.g. "Windows PowerShell", "Command Prompt")
    clean_disp = display_name.strip()
    for pat in _BLOCKED_DISPLAY_NAMES:
        if pat.search(clean_disp):
            return True, f"Application '{clean_disp}' is in the security denylist (matches {pat.pattern})."

    # 2. Check Store AppID and package name
    if is_store_app and app_id:
        lower_appid = app_id.lower()
        package_name = lower_appid.split("!")[0] if "!" in lower_appid else lower_appid
        # Check against denied store packages
        for pkg in _DENIED_STORE_PACKAGES:
            if pkg in package_name:
                return True, f"Store application '{app_id}' is in the security denylist."

    # 3. Check target executable stem and name
    if target_path:
        p = Path(target_path)
        stem = p.stem.lower()

        # Exact whole-word stem match
        if stem in DENIED_EXECUTABLE_STEMS:
            return True, f"Executable '{p.name}' is a restricted system utility and is denylisted."

        # Installer / uninstaller pattern match
        if _INSTALLER_PATTERN.search(stem):
            return True, f"Executable '{p.name}' appears to be an installer/uninstaller and is denylisted."

    return False, ""
