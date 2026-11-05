"""Small Win32/DWM helpers for window chrome (rounded corners, click-through)."""

from __future__ import annotations

import ctypes
import logging
import tkinter as tk

logger = logging.getLogger(__name__)

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001  # protected content (DRM playback) sets this
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def hwnd_of(window: tk.Misc) -> int:
    """Return the OS window handle for a Tk toplevel (its wrapper window)."""
    window.update_idletasks()
    client = window.winfo_id()
    parent = ctypes.windll.user32.GetParent(client)
    return parent or client


def round_corners(window: tk.Misc) -> None:
    """Ask DWM for Windows 11 rounded corners; silently no-ops elsewhere."""
    try:
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd_of(window), DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), ctypes.sizeof(pref)
        )
    except OSError:
        logger.debug("DWM rounded corners unavailable", exc_info=True)


def set_capture_exclusion(window: tk.Misc, excluded: bool) -> bool:
    """Toggle whether the window is hidden from screen capture (Win10 2004+)."""
    affinity = WDA_EXCLUDEFROMCAPTURE if excluded else WDA_NONE
    try:
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd_of(window), affinity))
    except OSError:
        logger.warning("SetWindowDisplayAffinity failed", exc_info=True)
        return False


_SINGLE_INSTANCE_MUTEX = "Global\\Transcreen.SingleInstance"
_ERROR_ALREADY_EXISTS = 183
_single_instance_handle: int | None = None


def claim_single_instance() -> bool:
    """Reserve the app's global mutex; False when another copy already runs.

    Two live copies each paint their own fullscreen overlay, so the same text
    ends up drawn twice at slightly different positions and sizes — it reads
    as jittering, doubled subtitles. The handle is kept alive for the process
    lifetime on purpose (Windows frees it on exit).
    """
    global _single_instance_handle
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX)
        if not handle:
            return True  # cannot tell: never block the app on this check
        if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _single_instance_handle = handle
        return True
    except OSError:
        logger.debug("single-instance check unavailable", exc_info=True)
        return True


def foreground_is_protected() -> bool:
    """True when the focused window is DRM-protected (Netflix, DRM video).

    Such apps refuse to render (black screen) while another process keeps a
    capture-related overlay in front of them, so the live loop pauses and the
    overlay hides while one is in focus. Our own overlay reports
    WDA_EXCLUDEFROMCAPTURE, never WDA_MONITOR, so it cannot self-trigger.
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        affinity = ctypes.c_uint(0)
        if not user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)):
            return False
        return affinity.value == WDA_MONITOR
    except OSError:
        return False


def exclude_from_capture(window: tk.Misc) -> bool:
    """Hide the window from every screen capture path (Win10 2004+).

    This is what lets the overlay stay visible to the user while our own
    ImageGrab sees the untouched screen behind it — no feedback loop.
    """
    return set_capture_exclusion(window, excluded=True)


def make_click_through(window: tk.Misc) -> None:
    """Let mouse events pass through the window (for the live overlay)."""
    try:
        user32 = ctypes.windll.user32
        hwnd = hwnd_of(window)
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except OSError:
        logger.warning("could not make overlay click-through", exc_info=True)
