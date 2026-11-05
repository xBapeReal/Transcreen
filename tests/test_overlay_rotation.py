"""Rotated-item geometry and the per-line color cache (no Tk, no network)."""

import math

import pytest
from PIL import Image, ImageDraw

from transcreen.app import (
    App,
    _color_key,
    _drop_duplicate_items,
    _line_vanished,
    _pending_region,
    _sticky_box,
)
from transcreen.ocr import OcrLine
from transcreen.overlay import OverlayItem, rotated_cover_corners
from transcreen.vision import StabilizeState, font_px_for_line, ink_fraction


def _pairs(flat: list[float]) -> list[tuple[float, float]]:
    return list(zip(flat[::2], flat[1::2], strict=True))


def test_cover_corners_upright_match_axis_aligned_rectangle():
    corners = _pairs(rotated_cover_corners(100, 50, 200, 15, 3, 0.0))
    expected = [(97, 35), (303, 35), (303, 65), (97, 65)]
    for (x, y), (ex, ey) in zip(corners, expected, strict=True):
        assert x == pytest.approx(ex)
        assert y == pytest.approx(ey)


def test_cover_corners_positive_angle_descends_and_n_points_up():
    a, b, c, d = _pairs(rotated_cover_corners(100, 50, 200, 15, 3, 30.0))
    # Baseline direction: the far end sits lower on screen (y grows downward).
    assert b[1] > a[1]
    # n side: first corner lies toward the glyph tops (above the anchor line).
    assert a[1] < 50
    assert d[1] > a[1]
    # The quad's edge length along the baseline equals run + 2 * pad_x.
    assert math.dist(a, b) == pytest.approx(206)
    assert math.dist(a, d) == pytest.approx(30)


def test_overlay_item_old_constructor_defaults():
    item = OverlayItem(
        left=1, top=2, width=3, height=4, text="x", bg="#000000", fg="#ffffff", font_px=12
    )
    assert item.angle_deg == 0.0
    assert item.ink_height == 0
    assert item.run_length == 0
    assert item.anchor_x == -1
    assert item.anchor_y == -1


def _build(lines, cached, frame, colors):
    blocks = [[line] for line in lines]
    texts = [line.text for line in lines]
    # No quantity affixes in these fixtures: the whole text is the core.
    affixes = [("", text, "") for text in texts]
    return App._build_items(App.__new__(App), blocks, texts, affixes, cached, frame, colors)


def test_build_items_normalizes_sentinels_and_sizes_font_from_ink(monkeypatch):
    monkeypatch.setattr(
        "transcreen.app.pick_colors", lambda frame, line: ("#111111", "#eeeeee", False)
    )
    frame = Image.new("RGB", (400, 300), "white")
    upright = OcrLine(text="Hello", left=10, top=20, width=100, height=24)
    rotated = OcrLine(
        text="Slanted",
        left=50,
        top=60,
        width=200,
        height=90,
        angle_deg=15.0,
        ink_height=22,
        run_length=180,
        anchor_x=55,
        anchor_y=100,
    )
    items = _build([upright, rotated], ["Merhaba", "Yatık"], frame, {})

    up, rot = items
    assert (up.angle_deg, up.ink_height, up.run_length) == (0.0, 24, 100)
    assert (up.anchor_x, up.anchor_y) == (10, 20 + 24 // 2)
    # Single-line em size is capped at 1.15x the box height (here the cap
    # binds: the ascender-only profile would ask for a larger em).
    assert up.font_px == min(font_px_for_line("Hello", 24), round(24 * 1.15))
    assert up.font_px < font_px_for_line("Hello", 24)
    assert (rot.angle_deg, rot.ink_height, rot.run_length) == (15.0, 22, 180)
    assert (rot.anchor_x, rot.anchor_y) == (55, 100)
    # The rotated line's AABB height (90) leaves the cap slack.
    assert rot.font_px == font_px_for_line("Slanted", 22)


def test_build_items_reuses_cached_colors_on_force_redraw(monkeypatch):
    calls: list[str] = []

    def fake_pick(frame, line):
        calls.append(line.text)
        return ("#101010", "#f0f0f0", False)

    monkeypatch.setattr("transcreen.app.pick_colors", fake_pick)
    frame = Image.new("RGB", (400, 300), "white")
    lines = [
        OcrLine(text="One", left=8, top=8, width=90, height=20),
        OcrLine(text="Two", left=8, top=40, width=90, height=20),
    ]
    colors: dict = {}
    first = _build(lines, ["Bir", "İki"], frame, colors)
    assert calls == ["One", "Two"]
    # Force-redraw rebuild: same lines, same dict -> zero pick_colors calls.
    second = _build(lines, ["Bir", "İki"], frame, colors)
    assert calls == ["One", "Two"]
    assert [(i.bg, i.fg) for i in second] == [(i.bg, i.fg) for i in first]
    # Pending lines are skipped and must not warm the cache.
    calls.clear()
    _build(lines, [None, "İki"], frame, colors)
    assert calls == []


def test_build_items_paragraph_block_becomes_wrapped_item(monkeypatch):
    monkeypatch.setattr(
        "transcreen.app.pick_colors", lambda frame, line: ("#111111", "#eeeeee", True)
    )
    frame = Image.new("RGB", (800, 600), "white")
    block = [
        OcrLine(text="First line", left=100, top=100, width=400, height=20, ink_height=16),
        OcrLine(text="second line.", left=100, top=126, width=300, height=20, ink_height=18),
    ]
    items = App._build_items(
        App.__new__(App),
        [block],
        ["First line second line."],
        [("", "First line second line.", "")],
        ["Birinci satır ikinci satır."],
        frame,
        {},
    )
    (item,) = items
    assert item.wrap_width == 400
    assert (item.left, item.top, item.width, item.height) == (100, 100, 400, 46)
    assert item.bold is True
    assert item.text == "Birinci satır ikinci satır."
    assert item.font_px == font_px_for_line("First line second line.", 18)


def test_duplicate_items_over_one_another_are_dropped():
    # Two OCR readings of one button both translate to "OYNA": only the
    # larger box may be drawn, or the label appears doubled on screen.
    big = OverlayItem(
        left=91, top=625, width=157, height=68, text="OYNA", bg="#000000", fg="#ffffff", font_px=53
    )
    small = OverlayItem(
        left=86, top=647, width=152, height=46, text="OYNA", bg="#000000", fg="#ffffff", font_px=45
    )
    elsewhere = OverlayItem(
        left=900, top=200, width=120, height=30, text="OYNA", bg="#000000", fg="#ffffff", font_px=25
    )
    kept = _drop_duplicate_items([small, big, elsewhere])
    assert len(kept) == 2  # the overlapping pair collapses, the distant one stays
    boxes = {(item.left, item.top, item.height) for item in kept}
    assert (91, 625, 68) in boxes  # the larger box survived
    assert elsewhere in kept


def test_different_texts_overlapping_both_survive():
    one = OverlayItem(
        left=100, top=100, width=200, height=30, text="OYNA", bg="#000", fg="#fff", font_px=25
    )
    two = OverlayItem(
        left=110, top=105, width=200, height=30, text="ÇIK", bg="#000", fg="#fff", font_px=25
    )
    assert len(_drop_duplicate_items([one, two])) == 2


def test_sticky_box_adopts_new_text_box_at_once():
    boxes: dict = {}
    key = (10, 20)
    long_line = OcrLine(text="I told you, I was at work.", left=330, top=650, width=700, height=40)
    assert _sticky_box(boxes, key, long_line, 100.0, long_line.text)[2] == 700
    # Same text, jittered box: the settled geometry holds.
    jitter = OcrLine(text=long_line.text, left=334, top=651, width=690, height=41)
    assert _sticky_box(boxes, key, jitter, 100.1, long_line.text) == (330, 650, 700, 40)
    # A different sentence at the spot must not inherit the old, wider cover:
    # that is the bar of background running past the end of a short line.
    short_line = OcrLine(text="Yes.", left=600, top=652, width=90, height=38)
    assert _sticky_box(boxes, key, short_line, 100.2, short_line.text) == (600, 652, 90, 38)


def test_ink_fraction_and_line_vanished_track_text_presence():
    frame = Image.new("RGB", (400, 100), (30, 30, 30))
    draw = ImageDraw.Draw(frame)
    draw.rectangle((30, 60, 250, 70), fill=(200, 40, 40))  # video content, not text
    line = OcrLine(text="Subtitle", left=20, top=20, width=300, height=40)
    assert ink_fraction(frame, line, "#ffffff") == 0.0
    key = (line.left // 32, line.top // 32)
    marks = {key: ("#ffffff", 0.33)}  # a third of the box was white text when painted
    assert _line_vanished(frame, marks, line)  # no white left: the subtitle went away
    draw.rectangle((30, 30, 250, 50), fill=(255, 255, 255))  # the text is (still) there
    assert ink_fraction(frame, line, "#ffffff") > 0.3
    assert not _line_vanished(frame, marks, line)
    assert not _line_vanished(frame, {}, line)  # no baseline: the timed TTL decides


def test_pending_region_covers_birth_held_lines():
    # The starvation bug this guards: a birth-held line on a static screen has
    # no dirty region of its own, so the recheck pass must derive its area
    # from the birth entry itself (or it never confirms, never translates).
    frame = Image.new("RGB", (800, 600), "white")
    born = OcrLine(text="New sign", left=300, top=200, width=120, height=20)
    stab = StabilizeState(births={(300 // 8, 200 // 8): (1, born)})
    region = _pending_region([], stab, frame)
    assert region is not None
    assert region.left <= 300 and region.right >= 420
    assert region.top <= 200 and region.bottom >= 220


def test_pending_region_empty_state_returns_none():
    frame = Image.new("RGB", (800, 600), "white")
    assert _pending_region([], StabilizeState(), frame) is None


def test_color_key_buckets_position():
    a = OcrLine(text="Same", left=100, top=200, width=50, height=20)
    b = OcrLine(text="Same", left=103, top=203, width=50, height=20)  # OCR jitter
    c = OcrLine(text="Same", left=140, top=200, width=50, height=20)
    assert _color_key(a) == _color_key(b)
    assert _color_key(a) != _color_key(c)
