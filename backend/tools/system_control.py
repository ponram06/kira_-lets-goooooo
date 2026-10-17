"""
system_control.py
=================
M2 system tools: volume, brightness, lock the PC, screenshot into
workspace/, and system info.

Risk (see policy.TOOL_REGISTRY):
    volume, brightness, system_info   LOW
    lock_pc, take_screenshot          MEDIUM, free_auto (ask in Restricted)

Hardware access is behind small helpers (_endpoint, _sbc, _lock_workstation,
_grab_screen) so tests replace them; tests never touch real audio, displays
or the session lock.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from backend import policy as policy_module
from backend.config_loader import load_config
from backend.policy import resolve_path

logger = logging.getLogger(__name__)


def _tools_cfg() -> dict:
    try:
        return load_config().get("tools", {}) or {}
    except Exception:
        return {}


def _clamp_pct(value: Any) -> int:
    return max(0, min(100, int(round(float(value)))))


# ── volume (pycaw) ──────────────────────────────────────────────────────────

def _endpoint() -> Any:
    """Default speakers' IAudioEndpointVolume."""
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def volume(action: str, level: Optional[int] = None, step: Optional[int] = None) -> str:
    """
    action: "up" | "down" | "set" | "mute" | "unmute" | "get"
    level:  0-100 for "set"; step: percent for up/down (config tools.volume_step)
    """
    act = str(action or "").strip().lower()
    if act not in ("up", "down", "set", "mute", "unmute", "get"):
        return "Error: volume action must be up, down, set, mute, unmute or get."
    step_pct = _clamp_pct(step if step is not None else _tools_cfg().get("volume_step", 10))
    try:
        ep = _endpoint()
        current = _clamp_pct(ep.GetMasterVolumeLevelScalar() * 100)
        if act == "get":
            muted = bool(ep.GetMute())
            return f"Volume is {current}%" + (" (muted)." if muted else ".")
        if act in ("mute", "unmute"):
            ep.SetMute(1 if act == "mute" else 0, None)
            return "Volume muted." if act == "mute" else f"Volume unmuted ({current}%)."
        if act == "set":
            if level is None:
                return "Error: give a level from 0 to 100."
            target = _clamp_pct(level)
        else:
            target = _clamp_pct(current + step_pct if act == "up" else current - step_pct)
        ep.SetMasterVolumeLevelScalar(target / 100.0, None)
        if target > 0 and act in ("up", "set"):
            ep.SetMute(0, None)
        return f"Volume set to {target}%."
    except Exception as exc:
        return f"Error: could not change volume ({type(exc).__name__})."


# ── brightness ──────────────────────────────────────────────────────────────

def _sbc() -> Any:
    import screen_brightness_control
    return screen_brightness_control


def brightness(action: str, level: Optional[int] = None, step: Optional[int] = None) -> str:
    """action: "up" | "down" | "set" | "get" (primary display)."""
    act = str(action or "").strip().lower()
    if act not in ("up", "down", "set", "get"):
        return "Error: brightness action must be up, down, set or get."
    step_pct = _clamp_pct(step if step is not None else _tools_cfg().get("brightness_step", 10))
    try:
        sbc = _sbc()
        values = sbc.get_brightness(display=0)
        current = _clamp_pct(values[0] if isinstance(values, list) else values)
        if act == "get":
            return f"Brightness is {current}%."
        if act == "set":
            if level is None:
                return "Error: give a level from 0 to 100."
            target = _clamp_pct(level)
        else:
            target = _clamp_pct(current + step_pct if act == "up" else current - step_pct)
        # Never go fully dark by accident
        target = max(target, 5)
        sbc.set_brightness(target, display=0)
        return f"Brightness set to {target}%."
    except Exception as exc:
        return ("Error: brightness control is not supported on this display "
                f"({type(exc).__name__}).")


# ── lock ────────────────────────────────────────────────────────────────────

def _lock_workstation() -> bool:
    return bool(ctypes.windll.user32.LockWorkStation())


def lock_pc() -> str:
    """Lock the Windows session (same as Win+L)."""
    try:
        if _lock_workstation():
            return "PC locked."
        return "Error: Windows refused to lock the session."
    except Exception as exc:
        return f"Error: could not lock the PC ({type(exc).__name__})."


# ── screenshot (GDI via ctypes, PNG via zlib — no extra packages) ───────────

def encode_png(width: int, height: int, bgra: bytes) -> bytes:
    """Encode top-down 32-bit BGRA pixels as an RGB PNG."""
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        row = bgra[y * stride:(y + 1) * stride]
        raw.append(0)                       # filter: none
        # BGRA → RGB
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        raw += rgb

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def _grab_screen() -> tuple[int, int, bytes]:
    """Capture the whole virtual desktop (all monitors) as BGRA."""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
    x, y = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w, h = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, w, h)
    try:
        gdi32.SelectObject(mem_dc, bitmap)
        SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
        gdi32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, x, y, SRCCOPY | CAPTUREBLT)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                        ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                        ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = w, -h          # negative height → top-down rows
        bmi.biPlanes, bmi.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem_dc, bitmap, 0, h, buf, ctypes.byref(bmi), 0)
        return w, h, buf.raw
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)


def take_screenshot(name: Optional[str] = None) -> str:
    """Save a screenshot of all monitors to workspace/<screenshots_dir>/ (PNG)."""
    folder_rel = str(_tools_cfg().get("screenshots_dir", "screenshots"))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c for c in str(name or "") if c.isalnum() or c in "-_")[:40]
    filename = f"{safe or 'screenshot'}_{stamp}.png"
    try:
        folder = resolve_path(folder_rel)          # must stay inside workspace/
        target = resolve_path(Path(folder_rel) / filename)
    except policy_module.PolicyError as exc:
        return f"Error: {exc}"
    try:
        w, h, pixels = _grab_screen()
        folder.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encode_png(w, h, pixels))
    except Exception as exc:
        return f"Error: screenshot failed ({type(exc).__name__})."
    rel = target.relative_to(policy_module.WORKSPACE_ROOT).as_posix()
    return f"Screenshot saved to workspace/{rel} ({w}x{h})."


# ── system info (psutil) ────────────────────────────────────────────────────

def system_info() -> str:
    """CPU load, RAM use and battery state."""
    import psutil
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    parts = [
        f"CPU {cpu:.0f}%",
        f"RAM {mem.percent:.0f}% used ({mem.used / 2**30:.1f} of {mem.total / 2**30:.1f} GB)",
    ]
    try:
        bat = psutil.sensors_battery()
    except Exception:
        bat = None
    if bat is not None:
        state = "charging" if bat.power_plugged else "on battery"
        left = ""
        if not bat.power_plugged and bat.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
            left = f", about {int(bat.secsleft // 3600)} h {int(bat.secsleft % 3600 // 60)} min left"
        parts.append(f"battery {bat.percent:.0f}% ({state}{left})")
    else:
        parts.append("no battery")
    return ", ".join(parts) + "."
