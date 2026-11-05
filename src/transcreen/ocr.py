"""OCR via the built-in Windows.Media.Ocr engine (no external installs).

All recognize() calls must come from a single worker thread; the engine is
created lazily on first use there.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

# Horizontal bands for parallel OCR: overlap covers lines cut by a seam, and
# each line is kept only by the band whose core contains its vertical center.
BAND_OVERLAP = 72
MIN_BAND_HEIGHT = 320

# Windows OCR reads small UI text noticeably better once glyphs are ~30 px tall,
# so modest images get upscaled 2x before recognition (full screens never are).
_UPSCALE_MAX_HEIGHT = 300
_UPSCALE_MAX_WIDTH = 3000
_UPSCALE_FACTOR = 2

_TERMINAL = (".", "!", "?", "…", ":", ";")
_BULLET_RE = re.compile(r"^\s*([•▪●◦‣*·—-]|\d+[.)])\s")

# Angles below this are treated as perfectly horizontal text (no mapping).
# 5 degrees: busy game backgrounds make the engine report spurious small
# angles for perfectly upright text, which flips the render path per pass.
_ANGLE_SNAP_DEG = 5.0


class OcrUnavailableError(RuntimeError):
    """Raised when no Windows OCR language pack is usable on this system."""


@dataclass(frozen=True, slots=True)
class OcrLine:
    """One recognized line with its bounding box in image pixel coordinates.

    left/top/width/height is always the axis-aligned bounding box in final
    image coordinates; for rotated lines it is the AABB of the four mapped
    corners of the deskewed line box.
    """

    text: str
    left: int
    top: int
    width: int
    height: int
    # Screen-space baseline angle in degrees: positive = baseline descends
    # left-to-right (screen y grows downward); |angle| < 2 is snapped to 0.
    angle_deg: float = 0.0
    # Median word height measured in deskewed space (true glyph ink height);
    # 0 means unknown -> use height.
    ink_height: int = 0
    # Text run length along the baseline (deskewed line width); 0 -> use width.
    run_length: int = 0
    # West-center of the line in final image coordinates (the natural text
    # draw anchor); -1 means unknown -> use (left, top + height // 2).
    anchor_x: int = -1
    anchor_y: int = -1


def merge_lines(lines: list[str]) -> str:
    """Join raw OCR lines into readable text.

    Wrapped lines of a paragraph are joined with spaces (hyphenated words are
    rejoined), while sentence ends, bullets and blank lines keep line breaks so
    the translation preserves the visible structure.
    """
    paragraphs: list[str] = []
    buf = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            if buf:
                paragraphs.append(buf)
                buf = ""
            continue
        if not buf:
            buf = line
        elif _BULLET_RE.match(line):
            paragraphs.append(buf)
            buf = line
        elif buf.endswith("-") and line[:1].islower():
            buf = buf[:-1] + line
        elif buf.endswith(_TERMINAL):
            paragraphs.append(buf)
            buf = line
        else:
            buf = buf + " " + line
    if buf:
        paragraphs.append(buf)
    return "\n".join(paragraphs)


# Convention verified empirically with synthetic rotated images (see
# tests/test_ocr_rotation.py): Windows.Media.Ocr returns word rects in a
# DESKEWED coordinate space and reports the rotation as OcrResult.text_angle.
# Text drawn rotated
# visually counterclockwise (PIL Image.rotate(+33), baseline ascending
# left-to-right) measured text_angle = -33.5; visually clockwise text
# (PIL rotate(-33), baseline descending) measured text_angle = +33.5. So
# text_angle is positive for text rotated CLOCKWISE on screen, and deskewed
# coordinates map back to real image coordinates by rotating them CLOCKWISE
# (screen sense) by text_angle around the center of the image the engine saw
# (match within 2-4 px at both line ends for both signs). Hence
# OcrLine.angle_deg = text_angle directly satisfies "positive = baseline
# descends left-to-right".
def rotate_point(x: float, y: float, cx: float, cy: float, deg: float) -> tuple[float, float]:
    """Rotate (x, y) around (cx, cy) by deg degrees clockwise on screen.

    Screen coordinates grow rightward and downward, so positive deg turns
    points clockwise as the user sees them (y-down flips the math-CCW matrix).
    """
    rad = math.radians(deg)
    cos, sin = math.cos(rad), math.sin(rad)
    dx, dy = x - cx, y - cy
    return cx + cos * dx - sin * dy, cy + sin * dx + cos * dy


class WindowsOcr:
    def __init__(self, preferred_lang: str = "auto") -> None:
        self.preferred_lang = preferred_lang
        self._engine: Any = None

    @staticmethod
    def available_languages() -> list[str]:
        from winsdk.windows.media.ocr import OcrEngine

        languages = OcrEngine.available_recognizer_languages
        return [lang.language_tag for lang in languages] if languages else []

    def set_language(self, tag: str) -> None:
        if tag != self.preferred_lang:
            self.preferred_lang = tag
            self._engine = None

    @property
    def language(self) -> str | None:
        if self._engine is None:
            return None
        lang = self._engine.recognizer_language
        return lang.language_tag if lang else None

    def _ensure_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        from winsdk.windows.globalization import Language
        from winsdk.windows.media.ocr import OcrEngine

        engine = None
        if self.preferred_lang != "auto":
            engine = OcrEngine.try_create_from_language(Language(self.preferred_lang))
            if engine is None:
                logger.warning("OCR language %s unavailable, falling back", self.preferred_lang)
        if engine is None:
            # Screens translated to Turkish are usually foreign-language, so
            # English is the best automatic pick when installed.
            engine = OcrEngine.try_create_from_language(Language("en-US"))
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            available = OcrEngine.available_recognizer_languages
            if available:
                engine = OcrEngine.try_create_from_language(available[0])
        if engine is None:
            raise OcrUnavailableError(
                "No Windows OCR language pack found. Install one under "
                "Settings > Time & Language > Language."
            )
        lang = engine.recognizer_language
        logger.info("OCR engine language: %s", lang.language_tag if lang else "unknown")
        self._engine = engine
        return engine

    def recognize_lines(self, img: Image.Image) -> list[OcrLine]:
        """Recognize text lines with bounding boxes in original image pixels."""
        engine = self._ensure_engine()
        from winsdk.windows.media.ocr import OcrEngine

        scale = 1.0
        if img.height < _UPSCALE_MAX_HEIGHT and img.width < _UPSCALE_MAX_WIDTH:
            scale = float(_UPSCALE_FACTOR)
        max_dim = OcrEngine.max_image_dimension
        if max(img.size) * scale > max_dim:
            scale = max_dim / max(img.size)
        if scale != 1.0:
            img = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                Image.Resampling.LANCZOS,
            )
        result = asyncio.run(self._recognize_async(engine, img))

        raw_angle = getattr(result, "text_angle", None)
        angle = 0.0 if raw_angle is None else float(raw_angle)
        if abs(angle) < _ANGLE_SNAP_DEG:
            angle = 0.0
        # The engine ran on the (possibly upscaled) image, so deskewed rects
        # rotate around THAT image's center; scale_line maps back afterwards.
        center_x, center_y = img.width / 2, img.height / 2

        lines: list[OcrLine] = []
        for line in result.lines:
            rects = [word.bounding_rect for word in line.words]
            if not rects:
                continue
            # Union box, run length, ink height and anchor in deskewed space.
            left = min(r.x for r in rects)
            top = min(r.y for r in rects)
            right = max(r.x + r.width for r in rects)
            bottom = max(r.y + r.height for r in rects)
            ink_height = statistics.median(r.height for r in rects)
            anchor_x, anchor_y = left, (top + bottom) / 2
            if angle == 0.0:
                box_left, box_top = int(left), int(top)
                box_width, box_height = int(right - left), int(bottom - top)
            else:
                corners = ((left, top), (right, top), (right, bottom), (left, bottom))
                mapped = [rotate_point(x, y, center_x, center_y, angle) for x, y in corners]
                xs = [x for x, _ in mapped]
                ys = [y for _, y in mapped]
                box_left, box_top = round(min(xs)), round(min(ys))
                box_width = max(1, round(max(xs) - min(xs)))
                box_height = max(1, round(max(ys) - min(ys)))
                anchor_x, anchor_y = rotate_point(anchor_x, anchor_y, center_x, center_y, angle)
            lines.append(
                scale_line(
                    OcrLine(
                        text=line.text,
                        left=box_left,
                        top=box_top,
                        width=box_width,
                        height=box_height,
                        angle_deg=angle,
                        ink_height=max(1, round(ink_height)),
                        run_length=max(1, round(right - left)),
                        anchor_x=round(anchor_x),
                        anchor_y=round(anchor_y),
                    ),
                    1.0 / scale,
                )
            )
        return lines

    def recognize(self, img: Image.Image) -> list[str]:
        """Return only the recognized text lines of a PIL image."""
        return [line.text for line in self.recognize_lines(img)]

    @staticmethod
    async def _recognize_async(engine: Any, img: Image.Image) -> Any:
        from winsdk.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winsdk.windows.storage.streams import DataWriter

        rgba = img.convert("RGBA")
        writer = DataWriter()
        # The stubs want Array[int]; plain bytes works at runtime (proven in selftest).
        writer.write_bytes(rgba.tobytes())  # type: ignore[arg-type]
        bitmap = SoftwareBitmap(BitmapPixelFormat.RGBA8, rgba.width, rgba.height)
        bitmap.copy_from_buffer(writer.detach_buffer())
        return await engine.recognize_async(bitmap)


def scale_line(line: OcrLine, factor: float) -> OcrLine:
    """Map a line's box back to original image coordinates after resizing.

    Angles survive uniform scaling unchanged; the unknown sentinels
    (ink_height/run_length 0, anchor -1) are preserved as-is.
    """
    if factor == 1.0:
        return line
    return OcrLine(
        text=line.text,
        left=round(line.left * factor),
        top=round(line.top * factor),
        width=max(1, round(line.width * factor)),
        height=max(1, round(line.height * factor)),
        angle_deg=line.angle_deg,
        ink_height=max(1, round(line.ink_height * factor)) if line.ink_height > 0 else 0,
        run_length=max(1, round(line.run_length * factor)) if line.run_length > 0 else 0,
        anchor_x=round(line.anchor_x * factor) if line.anchor_x >= 0 else -1,
        anchor_y=round(line.anchor_y * factor) if line.anchor_y >= 0 else -1,
    )


def band_ranges(height: int, max_workers: int) -> list[tuple[int, int, int, int]]:
    """(crop_top, crop_bottom, core_top, core_bottom) bands covering height.

    Cores tile the image exactly; crops extend half the overlap past the core
    so a line cut at a seam is fully visible in the neighboring band.
    """
    n = max(1, min(max_workers, height // MIN_BAND_HEIGHT))
    if n <= 1:
        return [(0, height, 0, height)]
    bands: list[tuple[int, int, int, int]] = []
    for i in range(n):
        core_top = round(i * height / n)
        core_bottom = round((i + 1) * height / n)
        crop_top = max(0, core_top - BAND_OVERLAP // 2)
        crop_bottom = min(height, core_bottom + BAND_OVERLAP // 2)
        bands.append((crop_top, crop_bottom, core_top, core_bottom))
    return bands


class ParallelOcr:
    """Windows OCR spread across horizontal bands on multiple engine instances.

    Measured on a typical desktop: a full 1080p frame drops from ~200 ms on one
    engine to ~90 ms on four. Each executor thread owns its own WindowsOcr.
    """

    def __init__(self, preferred_lang: str = "auto", workers: int = 4) -> None:
        self.preferred_lang = preferred_lang
        self._workers = max(1, workers)
        self._tls = threading.local()
        self._executor = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="ocr")

    @staticmethod
    def available_languages() -> list[str]:
        return WindowsOcr.available_languages()

    def _engine(self) -> WindowsOcr:
        engine = getattr(self._tls, "ocr", None)
        if engine is None:
            engine = self._tls.ocr = WindowsOcr(self.preferred_lang)
        return engine

    def recognize_lines(self, img: Image.Image) -> list[OcrLine]:
        bands = band_ranges(img.height, self._workers)
        futures = [self._executor.submit(self._ocr_band, img, band) for band in bands]
        return [line for future in futures for line in future.result()]

    def _ocr_band(self, img: Image.Image, band: tuple[int, int, int, int]) -> list[OcrLine]:
        crop_top, crop_bottom, core_top, core_bottom = band
        if crop_top == 0 and crop_bottom == img.height:
            crop = img
        else:
            crop = img.crop((0, crop_top, img.width, crop_bottom))
        kept: list[OcrLine] = []
        for line in self._engine().recognize_lines(crop):
            # Shift every y-field into full-image coordinates; angle, ink
            # height and run length are y-offset invariant.
            moved = OcrLine(
                text=line.text,
                left=line.left,
                top=line.top + crop_top,
                width=line.width,
                height=line.height,
                angle_deg=line.angle_deg,
                ink_height=line.ink_height,
                run_length=line.run_length,
                anchor_x=line.anchor_x,
                anchor_y=line.anchor_y + crop_top if line.anchor_y >= 0 else -1,
            )
            center = moved.top + moved.height / 2
            if core_top <= center < core_bottom or (
                core_bottom == img.height and center >= core_bottom
            ):
                kept.append(moved)
        return kept

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
