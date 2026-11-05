"""Global hotkeys via RegisterHotKey — no dependencies, no keyboard hook.

Callbacks fire on the hotkey thread; marshal them to the UI thread yourself.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes

logger = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

VK_T = 0x54
VK_L = 0x4C
VK_Q = 0x51
VK_S = 0x53

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


class HotkeyManager:
    def __init__(self) -> None:
        self._defs: list[tuple[int, int, str, Callable[[], None]]] = []
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self.failed: list[str] = []  # labels that could not be registered

    def add(self, mods: int, vk: int, label: str, callback: Callable[[], None]) -> None:
        self._defs.append((mods, vk, label, callback))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        for hk_id, (mods, vk, label, callback) in enumerate(self._defs, start=1):
            if user32.RegisterHotKey(None, hk_id, mods | MOD_NOREPEAT, vk):
                self._callbacks[hk_id] = callback
            else:
                self.failed.append(label)
                logger.warning("could not register hotkey %s (already in use?)", label)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                handler = self._callbacks.get(msg.wParam)
                if handler is not None:
                    try:
                        handler()
                    except Exception:
                        logger.exception("hotkey callback failed")
        for hk_id in self._callbacks:
            user32.UnregisterHotKey(None, hk_id)
