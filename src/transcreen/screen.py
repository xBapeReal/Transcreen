"""Screen geometry and capture helpers (DPI aware, multi-monitor)."""

from __future__ import annotations

import contextlib
import ctypes
import threading
from dataclasses import dataclass

from PIL import Image, ImageGrab

_tls = threading.local()

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def enable_dpi_awareness() -> None:
    """Must run before any window or screen grab so coordinates are physical pixels."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except OSError:
        with contextlib.suppress(OSError):
            ctypes.windll.user32.SetProcessDPIAware()


@dataclass(frozen=True, slots=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def as_bbox(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

    def intersect(self, other: Rect) -> Rect | None:
        left = max(self.left, other.left)
        top = max(self.top, other.top)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect(left, top, right - left, bottom - top)


def virtual_screen() -> Rect:
    gsm = ctypes.windll.user32.GetSystemMetrics
    return Rect(
        gsm(SM_XVIRTUALSCREEN),
        gsm(SM_YVIRTUALSCREEN),
        gsm(SM_CXVIRTUALSCREEN),
        gsm(SM_CYVIRTUALSCREEN),
    )


def grab_virtual_screen() -> Image.Image:
    return ImageGrab.grab(bbox=virtual_screen().as_bbox(), all_screens=True)


def grab_virtual_screen_fast() -> Image.Image:
    """BitBlt through a cached per-thread mss handle (~1.6x faster than ImageGrab).

    Honors WDA_EXCLUDEFROMCAPTURE just like ImageGrab (verified), so the
    overlay stays invisible to this capture path too.
    """
    try:
        from mss import MSS
    except ImportError:
        return grab_virtual_screen()
    sct = getattr(_tls, "sct", None)
    if sct is None:
        sct = _tls.sct = MSS()
    vs = virtual_screen()
    raw = sct.grab({"left": vs.left, "top": vs.top, "width": vs.width, "height": vs.height})
    return Image.frombuffer("RGB", raw.size, raw.bgra, "raw", "BGRX")


def grab_rect(rect: Rect) -> Image.Image:
    return ImageGrab.grab(bbox=rect.as_bbox(), all_screens=True)
