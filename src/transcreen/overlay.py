"""Fullscreen click-through overlay that paints translations over screen text.

The window covers the whole virtual screen, is transparent except for the
drawn items (color-key), lets every click pass through, and is excluded from
screen capture so the app's own grabs always see the untouched screen.
"""

from __future__ import annotations

import contextlib
import logging
import math
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont

from . import theme, winfx
from .screen import Rect

logger = logging.getLogger(__name__)

_KEY = "#010203"  # color-key treated as fully transparent
_MIN_FONT_PX = 8
_MAX_FONT_PX = 220
_WIDTH_SLACK = 1.12  # Turkish tends to run longer; allow a little overhang
_COVER_EXTRA = 1.08  # rotated covers: gentle widening only
_COVER_MAX = 1.45  # upright covers may grow this much to hide the source text


@dataclass(frozen=True, slots=True)
class OverlayItem:
    """One translated line, positioned in overlay/canvas pixel coordinates.

    left/top/width/height is always the axis-aligned box (the AABB for rotated
    lines). angle_deg follows the OCR convention: positive = baseline descends
    left-to-right on screen (screen y grows downward); 0 renders the plain
    upright path and ignores the rotation fields.
    """

    left: int
    top: int
    width: int
    height: int
    text: str
    bg: str
    fg: str
    font_px: int  # em size matching the source text's ink height (vision.font_px_for_line)
    angle_deg: float = 0.0
    ink_height: int = 0  # true glyph ink height; 0 -> use height
    run_length: int = 0  # text run along the baseline; 0 -> use width
    anchor_x: int = -1  # west-center draw anchor; -1 -> derive from the box
    anchor_y: int = -1
    bold: bool = False  # detected from ink density; rendered with a bold face
    wrap_width: int = 0  # >0: paragraph block, word-wrap the text to this width
    # Source line boxes (left, top, width, height) of a paragraph block: each
    # gets its own cover, so a short last line is not buried under a bar.
    source_lines: tuple[tuple[int, int, int, int], ...] = ()


@dataclass(slots=True)
class _Fitted:
    item: OverlayItem
    font: tkfont.Font
    text_width: int
    pad_y: int
    lines: list[tuple[str, int]]  # wrapped (text, pixel width) per rendered line
    linespace: int


def _wrap_words(font: tkfont.Font, text: str, width: int) -> list[tuple[str, int]]:
    """Greedy word wrap with the measured width of every rendered line."""
    lines: list[tuple[str, int]] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and font.measure(candidate) > width:
            lines.append((current, font.measure(current)))
            current = word
        else:
            current = candidate
    if current:
        lines.append((current, font.measure(current)))
    return lines


def rotated_cover_corners(
    anchor_x: float, anchor_y: float, run: float, half: float, pad_x: float, angle_deg: float
) -> list[float]:
    """Flat x/y corner list of the background cover for a rotated line.

    Screen coordinates (y down): d = (cos t, sin t) is the baseline direction,
    which descends for positive angle_deg; n = (sin t, -cos t) points from the
    baseline toward the glyph tops for small angles. The quad extends pad_x
    beyond both run ends and half to both sides of the anchor line.
    """
    theta = math.radians(angle_deg)
    dx, dy = math.cos(theta), math.sin(theta)
    nx, ny = math.sin(theta), -math.cos(theta)
    corners = (
        (anchor_x - dx * pad_x + nx * half, anchor_y - dy * pad_x + ny * half),
        (anchor_x + dx * (run + pad_x) + nx * half, anchor_y + dy * (run + pad_x) + ny * half),
        (anchor_x + dx * (run + pad_x) - nx * half, anchor_y + dy * (run + pad_x) - ny * half),
        (anchor_x - dx * pad_x - nx * half, anchor_y - dy * pad_x - ny * half),
    )
    return [coord for corner in corners for coord in corner]


def _anchor_of(item: OverlayItem) -> tuple[int, int]:
    """Concrete west-center anchor, deriving the -1 sentinel from the box."""
    x = item.anchor_x if item.anchor_x >= 0 else item.left
    y = item.anchor_y if item.anchor_y >= 0 else item.top + item.height // 2
    return x, y


class ScreenOverlay(tk.Toplevel):
    def __init__(self, master: tk.Misc, vs: Rect, *, capture_excluded: bool = True) -> None:
        super().__init__(master)
        self._vs = vs
        self.capture_excluded = False
        self._want_exclusion = capture_excluded

        self.overrideredirect(True)
        self.geometry(f"{vs.width}x{vs.height}+{vs.left}+{vs.top}")
        self.attributes("-topmost", True)
        self.configure(bg=_KEY)
        with contextlib.suppress(tk.TclError):
            self.attributes("-transparentcolor", _KEY)

        self._canvas = tk.Canvas(
            self, width=vs.width, height=vs.height, bg=_KEY, highlightthickness=0, bd=0
        )
        self._canvas.pack(fill="both", expand=True)
        self._fonts: dict[tuple[int, bool], tkfont.Font] = {}
        self._mapped = True
        self.after(60, self._apply_window_styles)

    def _apply_window_styles(self) -> None:
        winfx.make_click_through(self)
        if self._want_exclusion:
            self.capture_excluded = winfx.exclude_from_capture(self)
            if not self.capture_excluded:
                # ImageGrab skips layered windows by default, which still keeps
                # the overlay out of our own captures on this code path.
                logger.warning("capture exclusion unavailable; relying on layered-window skip")

    def set_items(self, items: list[OverlayItem]) -> None:
        if not self.winfo_exists():
            return  # teardown race: a paint posted after the window was destroyed
        canvas = self._canvas
        canvas.delete("item")
        self.after_idle(self.sync_visibility)
        fitted: list[_Fitted] = []
        for item in items:
            if item.wrap_width > 0:
                font, lines, linespace = self._fit_wrapped(item)
                fitted.append(_Fitted(item, font, 0, 3, lines, linespace))
                continue
            # Rotated lines fit along the baseline run, not the wider AABB.
            basis = item.width if item.angle_deg == 0.0 else (item.run_length or item.width)
            font, text_width = self._fit_font(item.text, basis, item.font_px, item.bold)
            ink = item.height if item.angle_deg == 0.0 else (item.ink_height or item.height)
            # Our glyphs may extend beyond the source ink box (descenders,
            # larger em): pad the cover vertically so nothing pokes out.
            pad_y = max(2, (font.metrics("linespace") - ink) // 2 + 1)
            fitted.append(_Fitted(item, font, text_width, pad_y, [], 0))
        # Two passes: all covers first, then all texts — so an overlapping
        # neighbor's cover can never paint over another item's text.
        for fit in fitted:
            item, text_width, pad_y = fit.item, fit.text_width, fit.pad_y
            if item.wrap_width > 0:
                # Each SOURCE line hides under its own box and each RENDERED
                # line gets a cover hugging its width. One block-sized
                # rectangle used to run past a short last line as a bar of
                # painted background with nothing written on it.
                source_lines = item.source_lines or (
                    (item.left, item.top, item.width, item.height),
                )
                for left, top, width, height in source_lines:
                    canvas.create_rectangle(
                        left - 2,
                        top - 2,
                        left + width + 2,
                        top + height + 2,
                        fill=item.bg,
                        outline=item.bg,
                        tags="item",
                    )
                for index, (_text, width) in enumerate(fit.lines):
                    y = item.top + index * fit.linespace
                    canvas.create_rectangle(
                        item.left - 3,
                        y - 1,
                        item.left + width + 5,
                        y + fit.linespace + 1,
                        fill=item.bg,
                        outline=item.bg,
                        tags="item",
                    )
            elif item.angle_deg == 0.0:
                # The cover must always reach past OUR text, or the tail of
                # the original English keeps peeking out from under a longer
                # Turkish line. _COVER_EXTRA only bounds the runaway case.
                cover_right = item.left + min(
                    max(item.width, text_width + 6), int(item.width * _COVER_MAX)
                )
                # Tight horizontal pad (2 px): wider pads read as a needlessly
                # long cover bar next to the neighboring UI.
                canvas.create_rectangle(
                    item.left - 2,
                    item.top - pad_y,
                    cover_right + 2,
                    item.top + item.height + pad_y,
                    fill=item.bg,
                    outline=item.bg,
                    tags="item",
                )
            else:
                run = item.run_length or item.width
                ink = item.ink_height or item.height
                anchor_x, anchor_y = _anchor_of(item)
                cover_run = max(run, min(text_width + 4, int(run * _COVER_EXTRA)))
                corners = rotated_cover_corners(
                    anchor_x, anchor_y, cover_run, ink / 2 + pad_y, 3, item.angle_deg
                )
                canvas.create_polygon(*corners, fill=item.bg, outline=item.bg, tags="item")
        for fit in fitted:
            item, font = fit.item, fit.font
            if item.wrap_width > 0:
                for index, (text, _width) in enumerate(fit.lines):
                    canvas.create_text(
                        item.left + 1,
                        item.top + index * fit.linespace,
                        text=text,
                        font=font,
                        fill=item.fg,
                        anchor="nw",
                        tags="item",
                    )
            elif item.angle_deg == 0.0:
                canvas.create_text(
                    item.left,
                    item.top + item.height // 2,
                    text=item.text,
                    font=font,
                    fill=item.fg,
                    anchor="w",
                    tags="item",
                )
            else:
                anchor_x, anchor_y = _anchor_of(item)
                # Tk's canvas-text angle is counterclockwise-positive on
                # screen while angle_deg is clockwise-positive, so negate.
                canvas.create_text(
                    anchor_x,
                    anchor_y,
                    text=item.text,
                    font=font,
                    fill=item.fg,
                    anchor="w",
                    angle=-item.angle_deg,
                    tags="item",
                )

    def clear(self) -> None:
        self._canvas.delete("item")

    def sync_visibility(self) -> None:
        """Keep the fullscreen window mapped ONLY while something is drawn.

        A permanently mapped topmost fullscreen window interferes with other
        apps' overlays, fullscreen-exclusive games and protected playback, so
        it is withdrawn whenever the canvas is empty. Re-mapping keeps the
        display affinity but the click-through style is re-applied to be safe.
        """
        wanted = bool(self._canvas.find_withtag("item") or self._canvas.find_withtag("toast"))
        if wanted == self._mapped:
            return
        with contextlib.suppress(tk.TclError):
            if wanted:
                self.deiconify()
                self._mapped = True
                self.after_idle(self._apply_window_styles)
            else:
                self.withdraw()
                self._mapped = False

    def _fit_font(
        self, text: str, width: int, start_px: int, bold: bool = False
    ) -> tuple[tkfont.Font, int]:
        """Start from the typography-matched em size, shrink until it fits."""
        px = min(max(start_px, _MIN_FONT_PX), _MAX_FONT_PX)
        fitted = self._font(px, bold)
        measured = fitted.measure(text)
        limit = int(width * _WIDTH_SLACK) + 6
        if measured > limit > 0:
            px = max(_MIN_FONT_PX, int(px * limit / measured))
            fitted = self._font(px, bold)
            while px > _MIN_FONT_PX and fitted.measure(text) > limit:
                px -= 1
                fitted = self._font(px, bold)
            measured = fitted.measure(text)
        return fitted, measured

    def _fit_wrapped(self, item: OverlayItem) -> tuple[tkfont.Font, list[tuple[str, int]], int]:
        """Shrink a paragraph font until the word-wrapped text fits its block.

        Returns the font, the wrapped lines with their measured widths, and
        the line spacing — the overlay draws the lines itself so every cover
        can hug its own line.
        """
        px = min(max(item.font_px, _MIN_FONT_PX), _MAX_FONT_PX)
        wrap_eff = max(40, item.wrap_width - 4)
        font = self._font(px, item.bold)
        linespace = font.metrics("linespace")
        est_height = len(_wrap_words(font, item.text, wrap_eff)) * linespace
        if est_height > item.height > 0:
            # Proportional first jump, then fine-step to the exact fit.
            px = max(_MIN_FONT_PX, int(px * math.sqrt(item.height / est_height)))
        while True:
            font = self._font(px, item.bold)
            linespace = font.metrics("linespace")
            lines = _wrap_words(font, item.text, wrap_eff)
            if len(lines) * linespace <= item.height + linespace * 0.6 or px <= _MIN_FONT_PX:
                return font, lines, linespace
            px -= 1

    def _font(self, px: int, bold: bool = False) -> tkfont.Font:
        key = (px, bold)
        cached = self._fonts.get(key)
        if cached is None:
            # Negative Tk font sizes mean pixels, which sidesteps DPI math.
            cached = tkfont.Font(
                root=self, family="Segoe UI", size=-px, weight="bold" if bold else "normal"
            )
            self._fonts[key] = cached
        return cached

    def show_toast(self, text: str, duration_ms: int = 4000) -> None:
        canvas = self._canvas
        canvas.delete("toast")
        self.after_idle(self.sync_visibility)
        x = self._vs.width // 2
        y = self._vs.height - 90
        label = canvas.create_text(
            x, y, text=text, font=("Segoe UI Semibold", 11), fill=theme.TEXT, tags="toast"
        )
        box = canvas.bbox(label)
        pill = canvas.create_rectangle(
            box[0] - 18,
            box[1] - 10,
            box[2] + 18,
            box[3] + 10,
            fill=theme.BG_DEEP,
            outline=theme.BORDER,
            tags="toast",
        )
        canvas.tag_raise(label, pill)
        if duration_ms > 0:

            def expire() -> None:
                canvas.delete("toast")
                self.sync_visibility()

            self.after(duration_ms, expire)
