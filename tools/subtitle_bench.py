"""Video-subtitle latency bench.

Draws a fake "video" (color-walking tiles, every frame differs) with timed
subtitles and measures, per subtitle, how long the app takes to paint a
Turkish cover, whether it ever does, how long the cover lingers after the
subtitle is gone, and how often the painted text flips.

usage: uv run python tools/subtitle_bench.py full|window [seconds_per_sub] [--debug]
(live screen bench: fullscreen window for ~45 s; the app is started in-process)
"""

from __future__ import annotations

import contextlib
import random
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PIL import ImageGrab

from transcreen.app import App
from transcreen.config import Config
from transcreen.screen import enable_dpi_awareness

_ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
REGIME = _ARGS[0] if _ARGS else "full"
ON_S = float(_ARGS[1]) if len(_ARGS) > 1 else 2.4
GAP_S = 0.6
WARMUP_S = 4.0
DEBUG = "--debug" in sys.argv
OUT = Path(__file__).resolve().parent / "bench"
OUT.mkdir(exist_ok=True)

SUBS: list[tuple[str, ...]] = [
    ("I didn't realise it was this late.",),
    ("No!",),
    ("Where were you last night?", "I told you, I was at work."),
    ("Yes.",),
    ("It's three o'clock in the morning.",),
    ("Hmm.",),
    ("Don't move. Stay right where you are.",),
    ("Sarah, get the car started.",),
    ("Run!",),
    ("We have to leave before they find us.",),
    ("This is going to be a long night.", "Grab your coat and follow me."),
    ("You can't be serious.",),
]

UI_LINES = [
    "Home",
    "Shorts",
    "Subscriptions",
    "Library",
    "History",
    "Watch later",
    "1,234,567 views",
    "Uploaded 3 days ago",
    "Share",
    "Download",
    "Save",
]


class Bench:
    def __init__(self, app: App) -> None:
        self.app = app
        self.root = app.root
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.sw, self.sh = sw, sh
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.geometry(f"{sw}x{sh}+0+0")
        self.win.attributes("-topmost", True)
        self.canvas = tk.Canvas(self.win, width=sw, height=sh, bg="#202020", highlightthickness=0)
        self.canvas.pack()
        if REGIME == "full":
            self.video = (0, 0, sw, sh)
        else:
            vw, vh = int(sw * 0.58), int(sh * 0.58)
            self.video = ((sw - vw) // 2, 90, (sw - vw) // 2 + vw, 90 + vh)
            # Static page chrome with SMALL text: pulls the median line height
            # down like a real browser page around a windowed player.
            for i, text in enumerate(UI_LINES):
                self.canvas.create_text(
                    24, 130 + i * 30, text=text, anchor="w", fill="#d0d0d0", font=("Segoe UI", -13)
                )
            self.canvas.create_text(
                self.video[0],
                self.video[3] + 30,
                text="Making of the season finale — full episode",
                anchor="w",
                fill="#e8e8e8",
                font=("Segoe UI", -18, "bold"),
            )
        self.tiles: list[tuple[int, list[int]]] = []
        step = 160
        x0, y0, x1, y1 = self.video
        for y in range(y0, y1, step):
            for x in range(x0, x1, step):
                rgb = [random.randint(30, 140) for _ in range(3)]
                tid = self.canvas.create_rectangle(
                    x, y, min(x + step, x1), min(y + step, y1), fill=self._hex(rgb), outline=""
                )
                self.tiles.append((tid, rgb))
        self.sub_items: list[int] = []
        self.sub_bbox: tuple[int, int, int, int] | None = None
        self.current: tuple[str, ...] | None = None
        self.results: list[dict] = []
        self.t_on = 0.0
        self.t_off = 0.0
        self.painted: list[tuple[float, str]] = []  # (t, text) distinct paints while on
        self.previous_texts: set[str] = set()
        self.after_off_gone: float | None = None
        self.index = -1
        self.shot_done = False
        self.root.after(33, self.animate)
        self.root.after(15, self.poll)
        self.root.after(int(WARMUP_S * 1000), self.next_sub)

    @staticmethod
    def _hex(rgb: list[int]) -> str:
        r, g, b = (max(0, min(255, v)) for v in rgb)
        return f"#{r:02x}{g:02x}{b:02x}"

    def animate(self) -> None:
        for tid, rgb in self.tiles:
            for c in range(3):
                rgb[c] = max(20, min(150, rgb[c] + random.randint(-14, 14)))
            self.canvas.itemconfigure(tid, fill=self._hex(rgb))
        self.root.after(33, self.animate)

    # ------------------------------------------------------------ subtitles
    def show_sub(self, lines: tuple[str, ...]) -> None:
        self.clear_sub()
        x0, y0, x1, y1 = self.video
        cx = (x0 + x1) // 2
        base_y = y1 - 110 - (len(lines) - 1) * 46
        boxes = []
        for i, line in enumerate(lines):
            y = base_y + i * 46
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                self.sub_items.append(
                    self.canvas.create_text(
                        cx + dx, y + dy, text=line, fill="#000000", font=("Segoe UI", -36, "bold")
                    )
                )
            tid = self.canvas.create_text(
                cx, y, text=line, fill="#ffffff", font=("Segoe UI", -36, "bold")
            )
            self.sub_items.append(tid)
            boxes.append(self.canvas.bbox(tid))
        self.sub_bbox = (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )

    def clear_sub(self) -> None:
        for tid in self.sub_items:
            self.canvas.delete(tid)
        self.sub_items = []

    def record_current(self, linger: float | None) -> None:
        if self.current is None:
            return
        self.results.append(
            {
                "sub": " / ".join(self.current),
                "first_paint": self.painted[0][0] if self.painted else None,
                "paints": self.painted,
                "linger": linger,
            }
        )
        self.previous_texts |= {t for _t, t in self.painted}
        self.current = None

    def next_sub(self) -> None:
        if self.current is not None:
            # The previous cover never went away before the next subtitle:
            # count the whole gap as linger.
            self.record_current(time.monotonic() - self.t_off)
        self.index += 1
        if self.index >= len(SUBS):
            self.root.after(int(GAP_S * 1000) + 1500, self.finish)
            return
        self.current = SUBS[self.index]
        self.show_sub(self.current)
        self.t_on = time.monotonic()
        self.painted = []
        self.after_off_gone = None
        self.root.after(int(ON_S * 1000), self.hide_sub)

    def hide_sub(self) -> None:
        self.clear_sub()
        self.t_off = time.monotonic()
        self.root.after(int(GAP_S * 1000), self.next_sub)

    # -------------------------------------------------------------- polling
    def overlapping_items(self):
        if self.sub_bbox is None:
            return []
        x0, y0, x1, y1 = self.sub_bbox
        out = []
        for it in self.app.last_items:
            ix0, iy0, ix1, iy1 = it.left, it.top, it.left + it.width, it.top + it.height
            if ix0 < x1 and ix1 > x0 and iy0 < y1 and iy1 > y0:
                out.append(it)
        return out

    def debug_lines(self, now: float) -> None:
        """Trace the stabilized OCR lines around the subtitle (DEBUG=1)."""
        if self.sub_bbox is None:
            return
        x0, y0, x1, y1 = self.sub_bbox
        stab = self.app.last_stab
        rows = []
        for ln in self.app.last_lines:
            if ln.top < y1 + 60 and ln.top + ln.height > y0 - 60:
                key = (ln.left // 8, ln.top // 8)
                miss = stab.misses.get(key)
                rows.append(f"{ln.text!r}@({ln.left},{ln.top},{ln.width}x{ln.height}) miss={miss}")
        births = [f"{h.text!r}x{c}" for c, h in stab.births.values()]
        line = f"t={now - self.t_on:5.2f}s on={bool(self.sub_items)} lines={rows} births={births}"
        if line[8:] != getattr(self, "_last_debug", "")[8:]:
            self._last_debug = line
            print("  DBG " + line, flush=True)

    def poll(self) -> None:
        now = time.monotonic()
        if DEBUG and self.current is not None:
            self.debug_lines(now)
        if self.current is not None:
            english = " ".join(self.current).casefold()
            items = self.overlapping_items()
            turkish = [it for it in items if it.text.strip().casefold() not in english]
            # Covers left over from the PREVIOUS subtitle do not count as a
            # paint of this one (they are its linger instead).
            fresh = [it for it in turkish if it.text not in self.previous_texts]
            if self.sub_items:  # subtitle is on
                if fresh:
                    text = " | ".join(sorted(it.text for it in fresh))
                    if not self.painted or self.painted[-1][1] != text:
                        self.painted.append((now - self.t_on, text))
                if self.index == 2 and not self.shot_done and now - self.t_on > 1.6:
                    self.shot_done = True
                    self.shoot()
            elif self.t_off and self.after_off_gone is None and not turkish:
                self.record_current(now - self.t_off)
        self.root.after(15, self.poll)

    def shoot(self) -> None:
        self.app.toggle_screenshot_mode()

        def grab() -> None:
            img = ImageGrab.grab(all_screens=True)
            img.save(OUT / f"{REGIME}_overlay.png")
            self.app.toggle_screenshot_mode()

        self.root.after(350, grab)

    def finish(self) -> None:
        if self.current is not None:  # last one never "went away" in time
            self.record_current(None)
        print(f"BENCH regime={REGIME} on={ON_S}s", flush=True)
        lat = [r["first_paint"] for r in self.results if r["first_paint"] is not None]
        misses = [r["sub"] for r in self.results if r["first_paint"] is None]
        lingers = [r["linger"] for r in self.results if r["linger"] is not None]
        flips = sum(max(0, len(r["paints"]) - 1) for r in self.results)
        for r in self.results:
            fp = "MISS" if r["first_paint"] is None else f"{r['first_paint'] * 1000:5.0f}ms"
            lg = "  -  " if r["linger"] is None else f"{r['linger'] * 1000:4.0f}ms"
            texts = " >> ".join(t for _t, t in r["paints"])
            flip_count = max(0, len(r["paints"]) - 1)
            print(f"  {fp} linger={lg} flips={flip_count}  {r['sub']!r} -> {texts!r}", flush=True)
        if lat:
            lat_sorted = sorted(lat)
            linger_median = (sorted(lingers)[len(lingers) // 2] if lingers else -1) * 1000
            linger_max = (max(lingers) if lingers else -1) * 1000
            print(
                f"SUMMARY first_paint median={lat_sorted[len(lat) // 2] * 1000:.0f}ms "
                f"max={max(lat) * 1000:.0f}ms  misses={len(misses)}/{len(self.results)}  "
                f"linger median={linger_median:.0f}ms max={linger_max:.0f}ms  flips={flips}",
                flush=True,
            )
        else:
            print(f"SUMMARY no paints at all; misses={len(misses)}", flush=True)
        with contextlib.suppress(Exception):
            self.win.destroy()
        self.app.quit()


def main() -> None:
    enable_dpi_awareness()
    app = App(Config.load())
    Bench(app)
    app.run()
    print("BENCH DONE", flush=True)


if __name__ == "__main__":
    main()
