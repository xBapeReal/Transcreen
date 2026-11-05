from transcreen.ocr import OcrLine, band_ranges, merge_lines, scale_line


def test_wrapped_sentence_joined():
    lines = ["The quick brown fox jumps over", "the lazy dog."]
    assert merge_lines(lines) == "The quick brown fox jumps over the lazy dog."


def test_hyphenated_word_rejoined():
    assert merge_lines(["transla-", "tion works"]) == "translation works"


def test_sentence_end_keeps_break():
    assert (
        merge_lines(["First sentence.", "Second sentence."]) == "First sentence.\nSecond sentence."
    )


def test_bullets_keep_breaks():
    lines = ["Features:", "• fast OCR", "• free translation"]
    assert merge_lines(lines) == "Features:\n• fast OCR\n• free translation"


def test_numbered_items_keep_breaks():
    lines = ["Steps", "1. do this", "2. then that"]
    assert merge_lines(lines) == "Steps\n1. do this\n2. then that"


def test_blank_lines_split_paragraphs():
    assert merge_lines(["a b", "", "c d"]) == "a b\nc d"


def test_empty_input():
    assert merge_lines([]) == ""


def test_scale_line_maps_boxes_back():
    line = OcrLine(text="x", left=100, top=50, width=200, height=30)
    half = scale_line(line, 0.5)
    assert (half.left, half.top, half.width, half.height) == (50, 25, 100, 15)
    assert half.text == "x"


def test_scale_line_identity_returns_same_object():
    line = OcrLine(text="x", left=1, top=2, width=3, height=4)
    assert scale_line(line, 1.0) is line


def test_ocr_line_base_constructor_gets_rotation_defaults():
    line = OcrLine(text="x", left=1, top=2, width=3, height=4)
    assert line.angle_deg == 0.0
    assert line.ink_height == 0
    assert line.run_length == 0
    assert (line.anchor_x, line.anchor_y) == (-1, -1)


def test_scale_line_scales_rotation_fields():
    line = OcrLine(
        text="x",
        left=100,
        top=50,
        width=200,
        height=30,
        angle_deg=-33.5,
        ink_height=26,
        run_length=537,
        anchor_x=160,
        anchor_y=330,
    )
    half = scale_line(line, 0.5)
    assert half.angle_deg == -33.5  # angles survive uniform scaling
    assert half.ink_height == 13
    assert half.run_length == 268
    assert (half.anchor_x, half.anchor_y) == (80, 165)


def test_scale_line_preserves_unknown_sentinels():
    half = scale_line(OcrLine(text="x", left=10, top=10, width=10, height=10), 0.5)
    assert half.ink_height == 0
    assert half.run_length == 0
    assert (half.anchor_x, half.anchor_y) == (-1, -1)


def test_band_ranges_single_for_short_images():
    assert band_ranges(300, 4) == [(0, 300, 0, 300)]


def test_band_ranges_cores_tile_exactly_with_overlapping_crops():
    bands = band_ranges(1080, 4)
    assert len(bands) == 3  # 1080 // 320
    assert bands[0][2] == 0
    assert bands[-1][3] == 1080
    for (_, _, _, prev_core_bottom), (_, _, next_core_top, _) in zip(
        bands, bands[1:], strict=False
    ):
        assert prev_core_bottom == next_core_top  # cores tile with no gap
    for crop_top, crop_bottom, core_top, core_bottom in bands:
        assert crop_top <= core_top
        assert crop_bottom >= core_bottom
    for (_, prev_crop_bottom, _, _), (next_crop_top, _, _, _) in zip(
        bands, bands[1:], strict=False
    ):
        assert prev_crop_bottom > next_crop_top  # crops overlap across seams
