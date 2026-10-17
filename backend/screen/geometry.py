"""
geometry.py
===========
Screen geometry for M6 screen control: DPI awareness, monitor rectangles and
mapping between the (downscaled) screenshot the vision model sees and real
virtual-desktop pixels.

All coordinates here are *physical* pixels of the Windows virtual desktop
(the primary monitor's top-left is (0, 0); monitors left of or above it have
negative coordinates). The process is made per-monitor DPI aware so that the
screenshot, GetSystemMetrics and SetCursorPos all agree.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int    # exclusive
    bottom: int   # exclusive

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


@dataclass(frozen=True)
class ScreenMap:
    """How a screenshot of the virtual desktop was scaled for the model."""
    virtual: Rect          # virtual desktop bounds (physical px)
    factor: int            # integer downscale factor (1 = full size)

    @property
    def image_size(self) -> tuple[int, int]:
        return (math.ceil(self.virtual.width / self.factor), math.ceil(self.virtual.height / self.factor))

    def to_screen(self, ix: int, iy: int) -> tuple[int, int]:
        """Image pixel → virtual-desktop pixel (centre of the scaled cell)."""
        half = self.factor // 2
        return (self.virtual.left + int(ix) * self.factor + half,
                self.virtual.top + int(iy) * self.factor + half)

    def to_image(self, x: int, y: int) -> tuple[int, int]:
        return ((x - self.virtual.left) // self.factor, (y - self.virtual.top) // self.factor)


def downscale_factor(width: int, height: int, max_width: int, max_height: int) -> int:
    """Smallest integer factor that fits the image inside max_width x max_height."""
    if width <= 0 or height <= 0:
        return 1
    return max(1, math.ceil(width / max(1, max_width)), math.ceil(height / max(1, max_height)))


def on_any_monitor(x: int, y: int, monitors: list[Rect]) -> bool:
    """True only if (x, y) is on a real monitor (not in a gap of the virtual desktop)."""
    return any(m.contains(x, y) for m in monitors)


# ── Windows specifics (not used in tests) ──────────────────────────────────

def make_dpi_aware() -> None:
    """Per-monitor v2 DPI awareness, falling back to older APIs."""
    if os.name != "nt":
        return
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception as exc:
            logger.debug("DPI awareness not set: %s", exc)


def virtual_desktop() -> Rect:
    if os.name != "nt":
        return Rect(0, 0, 1920, 1080)
    import ctypes
    u = ctypes.windll.user32
    x, y = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
    return Rect(x, y, x + u.GetSystemMetrics(78), y + u.GetSystemMetrics(79))


def monitors() -> list[Rect]:
    """Rectangles of all attached monitors (physical px)."""
    if os.name != "nt":
        return [Rect(0, 0, 1920, 1080)]
    import ctypes
    from ctypes import wintypes
    found: list[Rect] = []

    MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                         ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _cb(_hmon, _hdc, rect_ptr, _lp):
        r = rect_ptr.contents
        found.append(Rect(r.left, r.top, r.right, r.bottom))
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    return found or [virtual_desktop()]
