"""
capture.py
==========
Screenshots for M6 screen control: grab the virtual desktop, downscale by an
integer factor (fast, pure Python via memoryview), encode PNG in memory.
Screenshots are kept in memory only and never written to disk or logged.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from backend.screen.geometry import Rect, ScreenMap, downscale_factor


@dataclass
class Shot:
    smap: ScreenMap
    png: bytes
    digest: str          # sha256 of the scaled pixels (change detection)

    def data_url(self) -> str:
        return "data:image/png;base64," + base64.b64encode(self.png).decode("ascii")


def downscale_bgra(width: int, height: int, bgra: bytes, factor: int) -> tuple[int, int, bytes]:
    """Nearest-neighbour downscale by an integer factor."""
    if factor <= 1:
        return width, height, bgra
    px = memoryview(bgra).cast("I")
    rows = []
    for y in range(0, height, factor):
        rows.append(px[y * width:(y + 1) * width:factor].tobytes())
    new_w = len(range(0, width, factor))
    return new_w, len(rows), b"".join(rows)


def take(virtual: Rect, grab, encode_png, max_w: int = 1280, max_h: int = 800) -> Shot:
    """*grab* returns (w, h, bgra) for the virtual desktop; *encode_png(w, h, bgra)* → bytes."""
    w, h, raw = grab()
    factor = downscale_factor(w, h, max_w, max_h)
    sw, sh, small = downscale_bgra(w, h, raw, factor)
    smap = ScreenMap(Rect(virtual.left, virtual.top, virtual.left + w, virtual.top + h), factor)
    return Shot(smap=smap, png=encode_png(sw, sh, small), digest=hashlib.sha256(small).hexdigest())
