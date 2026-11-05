"""Rotated-text OCR: real-engine tests plus pure unit tests for the mapping.

The engine tests run the actual Windows.Media.Ocr engine (local only, no
network) and skip cleanly when winsdk or the Segoe UI font is unavailable.
"""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from transcreen.ocr import (
    OcrLine,
    OcrUnavailableError,
    ParallelOcr,
    WindowsOcr,
    rotate_point,
)

_FONT_PATH = Path("C:/Windows/Fonts/segoeui.ttf")

try:
    import winsdk.windows.media.ocr  # noqa: F401

    _HAVE_WINSDK = True
except Exception:  # pragma: no cover - depends on the machine
    _HAVE_WINSDK = False

needs_engine = pytest.mark.skipif(
    not (_HAVE_WINSDK and _FONT_PATH.exists()),
    reason="winsdk or Segoe UI font unavailable",
)

TEXT = "Rotated text should still be readable"
CANVAS = (1000, 700)
PASTE = (140, 160)
FONT_SIZE = 34
# Measured deskewed glyph ink height of 34 px Segoe UI (Windows OCR).
EXPECTED_INK = 27


def _render_rotated(pil_angle: float) -> tuple[Image.Image, tuple[float, float]]:
    """Canvas with TEXT rotated by PIL rotate(pil_angle), plus the true
    west-center (ink start) of the drawn line in canvas coordinates.

    PIL rotate(angle, expand=True) turns the image visually counterclockwise
    around the source center and recenters it in the expanded frame (verified
    to sub-pixel accuracy with a marker-dot experiment), so the expected
    position uses rotate_point with -pil_angle (rotate_point is CW-positive).
    """
    font = ImageFont.truetype(str(_FONT_PATH), FONT_SIZE)
    pad = 40
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    bb = probe.textbbox((pad, pad), TEXT, font=font)
    src = Image.new("RGB", (bb[2] + pad, bb[3] + pad), "white")
    draw = ImageDraw.Draw(src)
    draw.text((pad, pad), TEXT, font=font, fill="black")
    ink = draw.textbbox((pad, pad), TEXT, font=font)
    rot = src.rotate(pil_angle, expand=True, fillcolor="white", resample=Image.Resampling.BICUBIC)
    canvas = Image.new("RGB", CANVAS, "white")
    canvas.paste(rot, PASTE)
    wx, wy = rotate_point(ink[0], (ink[1] + ink[3]) / 2, src.width / 2, src.height / 2, -pil_angle)
    west = (
        wx - src.width / 2 + rot.width / 2 + PASTE[0],
        wy - src.height / 2 + rot.height / 2 + PASTE[1],
    )
    return canvas, west


@pytest.fixture(scope="module")
def engine() -> WindowsOcr:
    ocr = WindowsOcr()
    try:
        ocr._ensure_engine()
    except OcrUnavailableError as exc:
        pytest.skip(f"no Windows OCR language pack: {exc}")
    return ocr


@needs_engine
@pytest.mark.parametrize("pil_angle", [33.0, -33.0], ids=["ccw_ascending", "cw_descending"])
def test_rotated_text_recognized_and_mapped(engine: WindowsOcr, pil_angle: float) -> None:
    canvas, west = _render_rotated(pil_angle)
    lines = engine.recognize_lines(canvas)
    matches = [line for line in lines if "readable" in line.text.lower()]
    assert matches, f"rotated text not recognized, got: {[line.text for line in lines]}"
    line = matches[0]
    assert abs(abs(line.angle_deg) - 33.0) <= 5.0
    # Sign convention: positive angle_deg = baseline descends left-to-right,
    # which is what a negative (visually clockwise) PIL rotation draws.
    if pil_angle < 0:
        assert line.angle_deg > 0
    else:
        assert line.angle_deg < 0
    assert line.ink_height > 0
    assert abs(line.ink_height - EXPECTED_INK) <= EXPECTED_INK * 0.35
    assert math.dist((line.anchor_x, line.anchor_y), west) <= 40.0


def test_rotate_point_clockwise_screen_convention() -> None:
    # +90 deg takes a point right of center to straight below it (y grows
    # downward on screen), i.e. clockwise as the user sees it.
    assert rotate_point(10.0, 0.0, 0.0, 0.0, 90.0) == pytest.approx((0.0, 10.0))
    # -90 deg is counterclockwise on screen: right of center goes above.
    assert rotate_point(10.0, 0.0, 0.0, 0.0, -90.0) == pytest.approx((0.0, -10.0))


def test_rotate_point_center_identity_and_round_trip() -> None:
    assert rotate_point(50.0, 60.0, 50.0, 60.0, 77.0) == pytest.approx((50.0, 60.0))
    assert rotate_point(1.0, 2.0, 3.0, 4.0, 360.0) == pytest.approx((1.0, 2.0))
    x, y = rotate_point(12.0, 34.0, 50.0, 60.0, 33.0)
    assert rotate_point(x, y, 50.0, 60.0, -33.0) == pytest.approx((12.0, 34.0))


def _band_ocr_with_fake(line: OcrLine) -> list[OcrLine]:
    """Run ParallelOcr._ocr_band with a fake engine returning one line."""
    parallel = ParallelOcr(workers=1)
    try:
        parallel._tls.ocr = SimpleNamespace(recognize_lines=lambda img: [line])
        img = Image.new("RGB", (400, 700), "white")
        return parallel._ocr_band(img, (100, 500, 120, 480))
    finally:
        parallel.shutdown()


def test_ocr_band_shifts_all_y_fields_and_passes_rotation_through() -> None:
    kept = _band_ocr_with_fake(
        OcrLine(
            text="tilted",
            left=30,
            top=50,
            width=200,
            height=20,
            angle_deg=12.5,
            ink_height=18,
            run_length=210,
            anchor_x=31,
            anchor_y=60,
        )
    )
    assert len(kept) == 1
    moved = kept[0]
    assert (moved.top, moved.anchor_y) == (150, 160)
    assert (moved.left, moved.anchor_x) == (30, 31)
    assert moved.angle_deg == 12.5
    assert moved.ink_height == 18
    assert moved.run_length == 210


def test_ocr_band_keeps_unknown_anchor_sentinel() -> None:
    kept = _band_ocr_with_fake(OcrLine(text="x", left=0, top=50, width=10, height=20))
    assert len(kept) == 1
    assert (kept[0].anchor_x, kept[0].anchor_y) == (-1, -1)
    assert kept[0].top == 150
