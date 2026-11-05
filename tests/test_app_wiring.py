"""App-side wiring rules: quantity affixes, junk/blank guards, model check.

Pure-function tests (no Tk, no network, no model): they call App._build_items
on a bare instance and the module-level helpers directly.
"""

from PIL import Image

from transcreen.app import App, _drop_oversized_junk, _local_model_installed, _region_blank
from transcreen.ocr import OcrLine


def _build(blocks, texts, affixes, cached, frame, colors):
    return App._build_items(App.__new__(App), blocks, texts, affixes, cached, frame, colors)


def test_build_items_reattaches_quantity_affixes(monkeypatch):
    monkeypatch.setattr(
        "transcreen.app.pick_colors", lambda frame, line: ("#111111", "#eeeeee", False)
    )
    frame = Image.new("RGB", (400, 100), "white")
    line = OcrLine(text="x2 BANDAGES", left=10, top=10, width=200, height=20)
    (item,) = _build([[line]], ["x2 BANDAGES"], [("x2 ", "BANDAGES", "")], ["BANDAJ"], frame, {})
    assert item.text == "x2 BANDAJ"


def test_build_items_skips_echoed_core_translation(monkeypatch):
    monkeypatch.setattr(
        "transcreen.app.pick_colors", lambda frame, line: ("#111111", "#eeeeee", False)
    )
    frame = Image.new("RGB", (400, 100), "white")
    line = OcrLine(text="x3 EKMEK", left=10, top=10, width=200, height=20)
    # The CORE echoing back means "already Turkish": no cover, affix included.
    items = _build([[line]], ["x3 EKMEK"], [("x3 ", "EKMEK", "")], ["EKMEK"], frame, {})
    assert items == []


def test_drop_oversized_junk_drops_letter_poor_giants():
    small = [OcrLine(text=f"Line {i}", left=0, top=30 * i, width=100, height=20) for i in range(3)]
    junk = OcrLine(text="HP", left=300, top=0, width=200, height=90)
    big_real = OcrLine(text="INVENTORY", left=300, top=200, width=300, height=90)
    kept = _drop_oversized_junk(small + [junk, big_real])
    assert junk not in kept
    assert big_real in kept  # >4 letters: genuine big text survives
    assert all(line in kept for line in small)


def test_drop_oversized_junk_no_median_no_drop():
    lines = [OcrLine(text="OK", left=0, top=0, width=50, height=20)]
    assert _drop_oversized_junk(lines) == lines
    assert _drop_oversized_junk([]) == []


def test_region_blank_uniform_vs_textured():
    frame = Image.new("RGB", (200, 100), (30, 30, 30))
    for x in range(60, 120, 2):  # noisy stripes = "something is still drawn"
        for y in range(20, 40):
            frame.putpixel((x, y), (240, 240, 240))
    blank_line = OcrLine(text="Gone", left=0, top=50, width=80, height=30)
    inked_line = OcrLine(text="Here", left=55, top=15, width=70, height=30)
    off_screen = OcrLine(text="Off", left=500, top=500, width=40, height=20)
    assert _region_blank(frame, blank_line) is True
    assert _region_blank(frame, inked_line) is False
    assert _region_blank(frame, off_screen) is True


def test_local_model_installed(tmp_path):
    assert _local_model_installed(tmp_path / "missing") is False
    model_dir = tmp_path / "en_tr"
    (model_dir / "pkg" / "model").mkdir(parents=True)
    assert _local_model_installed(model_dir) is False  # dirs but no payload
    (model_dir / "pkg" / "model" / "model.bin").write_bytes(b"x")
    assert _local_model_installed(model_dir) is True
