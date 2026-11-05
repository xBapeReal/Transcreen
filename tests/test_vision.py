from PIL import Image, ImageDraw, ImageFont

from transcreen.ocr import OcrLine
from transcreen.vision import (
    SIG_H,
    SIG_W,
    TILE,
    StabilizeState,
    dirty_rect,
    fix_ocr_digits,
    font_px_for_line,
    frame_signature,
    group_blocks,
    is_probable_word,
    is_real_text,
    looks_turkish,
    median_line_height,
    merge_row_fragments,
    pick_colors,
    reading_score,
    reconcile_selection,
    signatures_differ,
    split_quantity_affixes,
    stabilize_lines,
)


def test_identical_frames_do_not_differ():
    a = Image.new("RGB", (400, 300), "white")
    assert not signatures_differ(frame_signature(a), frame_signature(a.copy()))


def test_changed_frame_differs():
    a = Image.new("RGB", (400, 300), "white")
    b = a.copy()
    ImageDraw.Draw(b).rectangle((50, 50, 350, 250), fill="black")
    assert signatures_differ(frame_signature(a), frame_signature(b))


def test_missing_signature_differs():
    assert signatures_differ(None, b"x")
    assert signatures_differ(b"x", None)


def test_pick_colors_dark_text_on_white():
    img = Image.new("RGB", (200, 40), "white")
    ImageDraw.Draw(img).rectangle((30, 12, 120, 28), fill=(10, 10, 10))
    line = OcrLine(text="Hi", left=20, top=8, width=160, height=24)
    bg, fg, _bold = pick_colors(img, line)
    assert bg == "#ffffff"
    assert int(fg[1:3], 16) < 100  # text color is dark


def test_pick_colors_degenerate_box_is_safe():
    img = Image.new("RGB", (50, 20), "white")
    line = OcrLine(text="x", left=49, top=19, width=0, height=0)
    bg, fg, bold = pick_colors(img, line)
    assert bg.startswith("#") and fg.startswith("#")
    assert bold is False


def _render_text_line(font_file: str) -> tuple[Image.Image, OcrLine]:
    img = Image.new("RGB", (620, 50), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_file, 22)
    draw.text((14, 12), "Settings and privacy options", fill="black", font=font)
    box = draw.textbbox((14, 12), "Settings and privacy options", font=font)
    line = OcrLine(
        text="Settings and privacy options",
        left=box[0],
        top=box[1],
        width=box[2] - box[0],
        height=box[3] - box[1],
    )
    return img, line


def test_pick_colors_detects_bold_weight():
    img, line = _render_text_line("segoeuib.ttf")
    _bg, _fg, bold = pick_colors(img, line)
    assert bold is True


def test_pick_colors_regular_weight_not_bold():
    img, line = _render_text_line("segoeui.ttf")
    _bg, _fg, bold = pick_colors(img, line)
    assert bold is False


def test_group_blocks_merges_left_aligned_paragraph():
    lines = [
        OcrLine(text="First line of the paragraph", left=100, top=100, width=400, height=20),
        OcrLine(text="second line continues here", left=102, top=126, width=380, height=20),
        OcrLine(text="third line ends it.", left=100, top=152, width=250, height=20),
    ]
    blocks = group_blocks(lines)
    assert len(blocks) == 1
    assert len(blocks[0]) == 3


def test_group_blocks_merges_center_aligned_subtitles():
    # Subtitle rows share a center, not a left edge.
    lines = [
        OcrLine(text="It's a new night. Read the note", left=160, top=760, width=600, height=34),
        OcrLine(text="behind the counter.", left=350, top=804, width=220, height=34),
    ]
    blocks = group_blocks(lines)
    assert len(blocks) == 1


def test_group_blocks_keeps_columns_apart():
    lines = [
        OcrLine(text="Left menu", left=20, top=100, width=120, height=18),
        OcrLine(text="Right content", left=600, top=100, width=200, height=18),
    ]
    assert len(group_blocks(lines)) == 2


def test_group_blocks_rotated_lines_stay_alone():
    lines = [
        OcrLine(text="Upright", left=100, top=100, width=100, height=20),
        OcrLine(text="Slanted", left=100, top=124, width=100, height=20, angle_deg=20.0),
    ]
    assert len(group_blocks(lines)) == 2


def test_merge_row_fragments_rejoins_spaced_subtitle():
    # Real case from a game screenshot: one sentence split into fragments by
    # wide word spacing, on two center-aligned rows. Word gaps of ~1.5x the
    # line height stay below the 1.8x merge limit.
    row1 = [
        OcrLine(text="It's", left=160, top=760, width=60, height=34),
        OcrLine(text="a new night.", left=270, top=760, width=250, height=34),
        OcrLine(text="Read", left=570, top=760, width=100, height=34),
        OcrLine(text="the note", left=720, top=762, width=140, height=32),
    ]
    row2 = [
        OcrLine(text="behind the", left=330, top=808, width=200, height=34),
        OcrLine(text="counter.", left=580, top=808, width=140, height=34),
    ]
    merged = merge_row_fragments(row1 + row2)
    assert len(merged) == 2
    texts = sorted(m.text for m in merged)
    assert texts == ["It's a new night. Read the note", "behind the counter."]
    top_row = next(m for m in merged if m.text.startswith("It's"))
    assert top_row.left == 160
    assert top_row.left + top_row.width == 860


def test_merge_row_fragments_keeps_distant_columns_apart():
    lines = [
        OcrLine(text="File", left=20, top=10, width=40, height=16),
        OcrLine(text="Help", left=600, top=10, width=40, height=16),  # far away
    ]
    assert len(merge_row_fragments(lines)) == 2


def test_merge_row_fragments_two_height_gap_stays_split():
    # Merge brake: a ~2.0x line-height gap is two separate UI fields, not one
    # sentence (the old 2.2x limit used to glue them together).
    lines = [
        OcrLine(text="Inventory", left=100, top=100, width=140, height=34),
        OcrLine(text="slots", left=310, top=100, width=80, height=34),  # gap 70
    ]
    assert len(merge_row_fragments(lines)) == 2


def test_fix_ocr_digits_repairs_letter_flanked_digits():
    assert fix_ocr_digits("It's a new ni9ht.") == "It's a new night."
    assert fix_ocr_digits("HE1LO") == "HEILO"  # uppercase context maps to uppercase


def test_fix_ocr_digits_leaves_real_alphanumerics_alone():
    assert fix_ocr_digits("Windows 11") == "Windows 11"
    assert fix_ocr_digits("R2D2 v2") == "R2D2 v2"
    assert fix_ocr_digits("Port 8080") == "Port 8080"


def test_reconcile_selection_keeps_line_over_garbled_fragments():
    old = OcrLine(
        text="The quick brown fox jumps over the lazy dog near the river",
        left=20,
        top=22,
        width=572,
        height=23,
    )
    fresh = [
        OcrLine(text="The quick", left=20, top=22, width=93, height=23),
        OcrLine(text="brown fox •um s over the", left=121, top=22, width=246, height=18),
        OcrLine(text="azy dog near the river", left=380, top=22, width=212, height=23),
    ]
    result = reconcile_selection([old], fresh)
    assert result == [old]


def test_reconcile_selection_adopts_genuinely_new_text():
    old = OcrLine(text="Completely unrelated sentence", left=20, top=22, width=400, height=23)
    fresh = [OcrLine(text="Brand new words appeared", left=20, top=22, width=380, height=23)]
    result = reconcile_selection([old], fresh)
    assert result == fresh


def test_dirty_rect_localizes_change():
    prev = bytes(SIG_W * SIG_H)
    curr = bytearray(prev)
    for y in range(2 * TILE, 3 * TILE):
        for x in range(4 * TILE, 6 * TILE):
            curr[y * SIG_W + x] = 255
    rect, fraction = dirty_rect(prev, bytes(curr), 1920, 1080)
    assert rect is not None
    # Changed tiles tx 4..5, ty 2 map to x 480..720, y 240..360 (10x scale).
    assert rect.left <= 480 and rect.right >= 720
    assert rect.top <= 240 and rect.bottom >= 360
    assert rect.width < 960  # localized, nowhere near full frame
    assert 0 < fraction < 0.1


def test_dirty_rect_identical_returns_none():
    sig = bytes(SIG_W * SIG_H)
    rect, fraction = dirty_rect(sig, sig, 1920, 1080)
    assert rect is None
    assert fraction == 0.0


def test_dirty_rect_no_previous_returns_full():
    sig = bytes(SIG_W * SIG_H)
    rect, fraction = dirty_rect(None, sig, 1920, 1080)
    assert rect is not None
    assert (rect.left, rect.top, rect.width, rect.height) == (0, 0, 1920, 1080)
    assert fraction == 1.0


def test_font_px_caps_only():
    assert font_px_for_line("HELLO WORLD", 72) == 100  # 72 / 0.72


def test_font_px_mixed_zones():
    # Validated against a real Windows OCR box: 30px em -> 29px box.
    assert font_px_for_line("Settings", 29) == 31


def test_font_px_xheight_only():
    assert font_px_for_line("sworn m", 26) == 50  # 26 / 0.52


def test_font_px_clamped():
    assert font_px_for_line("A", 2) == 8
    assert font_px_for_line("A", 900) == 240


# ------------------------------------------------------------- is_real_text


def test_is_real_text_filters_numeric_counters():
    # Real-game case: a "200 / 200" HP counter OCR'd with letter lookalikes
    # was translated to nonsense ("evet"); normalized it is pure digits.
    assert not is_real_text("200 / 200")
    assert not is_real_text("2OO / 2OO")
    assert not is_real_text("2OO/2OO")
    assert not is_real_text("l5 / 2O")


def test_is_real_text_filters_tiny_fragments():
    assert not is_real_text("I.E.")  # once rendered as a giant "YANİ"
    assert not is_real_text("oo")
    assert not is_real_text("x2")
    assert not is_real_text("+15%")
    assert not is_real_text("")


def test_is_real_text_keeps_real_words():
    assert is_real_text("Inspect")
    assert is_real_text("Long day")
    assert is_real_text("İki kelime")
    assert is_real_text("Yes")  # exactly three letters
    # Whitelisted two-letter utterances are real subtitles/buttons, not
    # fragments: the three-letter floor silently dropped "No!" in videos.
    assert is_real_text("No!")
    assert is_real_text("OK")
    assert not is_real_text("Oo!")  # two letters, not a word


def test_is_probable_word_exempts_real_short_lines_only():
    assert is_probable_word("Yes.")
    assert is_probable_word("Run!")
    assert is_probable_word("GO")
    assert is_probable_word("Hey")
    assert not is_probable_word("HP")  # vowelless icon misread
    assert not is_probable_word("eøø")  # exotic salad
    assert not is_probable_word("O O O")


def test_stabilize_birth_confirms_despite_box_jitter():
    # Over video the box of a brand-new line jitters by more than the 8 px
    # vote bucket between passes; the second sighting must still confirm it
    # (an exact-bucket match restarted the count and subtitles never showed).
    first = OcrLine(text="Where were you last night?", left=600, top=900, width=400, height=36)
    jittered = OcrLine(text="Where were you last night?", left=611, top=909, width=396, height=35)
    merged, state = _stab([], [first])
    assert merged == []  # held for a confirming sighting
    merged, state = _stab([], [jittered], state)
    assert merged == [jittered]
    assert state.births == {}


def test_is_real_text_normalizes_only_confusable_only_tokens():
    # "HP" contains non-confusable letters, so the token stays alphanumeric;
    # two letters are still below the three-letter minimum.
    assert not is_real_text("HP 200")
    assert is_real_text("Level 12 reached")


def test_is_real_text_still_rejects_vowelless_junk():
    assert not is_real_text("vwWlk")  # 4+ letters without a vowel: misread


def test_is_real_text_rejects_icon_letter_rows():
    # Real-game case: an empty inventory slot row OCR'd as "O O O" rendered a
    # giant junk subtitle.
    assert not is_real_text("O O O")
    assert not is_real_text("O O O O")
    assert not is_real_text("I I I")
    assert not is_real_text("A A A")  # repetition rule, even off-confusables
    assert not is_real_text("o l o l")  # confusable-only short tokens
    assert not is_real_text("OO ll OO")


def test_is_real_text_repetition_must_be_consecutive():
    # Three "I" words scattered through a real sentence are not an icon row.
    assert is_real_text("I know I said I would")


# ------------------------------------------------------------ looks_turkish


def test_looks_turkish_by_specific_characters():
    assert looks_turkish("Merhaba dünya")
    assert looks_turkish("Çıkış")
    assert looks_turkish("Kapıyı aç")


def test_looks_turkish_by_stopword_ratio():
    assert looks_turkish("bu bir test")
    assert looks_turkish("evet tamam")  # two stopwords: clearly Turkish
    assert looks_turkish("HAYIR evet")  # Turkish I-lowering still finds "hayır"
    assert looks_turkish("bunu al ve bu olsun")  # ve+bu = 2 hits, ratio ok


def test_looks_turkish_rejects_english():
    assert not looks_turkish("Hello world")
    assert not looks_turkish("Read the note")
    assert not looks_turkish("")
    # A stray stopword lookalike in long English text stays below 25%.
    assert not looks_turkish("Ben walked to the store with five other people")
    # Single-collision English must NOT be skipped (this silently blocked
    # short game lines from ever translating): one stopword is not Turkish.
    assert not looks_turkish("Ben walks in")
    assert not looks_turkish("var x = 1")
    assert not looks_turkish("tamam")  # single word: ambiguous, translate it


# --------------------------------------------------- split_quantity_affixes


def test_split_quantity_affixes_exact_cases():
    assert split_quantity_affixes("x2 BANDAGES") == ("x2 ", "BANDAGES", "")
    assert split_quantity_affixes("Ammo 30") == ("", "Ammo", " 30")
    assert split_quantity_affixes("Plain text") == ("", "Plain text", "")


def test_split_quantity_affixes_more_shapes():
    assert split_quantity_affixes("3x Medkit") == ("3x ", "Medkit", "")
    assert split_quantity_affixes("x2 Rusty Key 3") == ("x2 ", "Rusty Key", " 3")
    assert split_quantity_affixes("12 45 Shells x8") == ("12 45 ", "Shells", " x8")
    assert split_quantity_affixes("x2") == ("", "x2", "")  # nothing left to peel


def test_split_quantity_affixes_reassembles_verbatim():
    for text in ["x2  BANDAGES", " Ammo  30 ", "x2 30", "HP 200/200", "Plain text"]:
        prefix, core, suffix = split_quantity_affixes(text)
        assert prefix + core + suffix == text


# ------------------------------------------------------- median_line_height


def test_median_line_height():
    assert median_line_height([]) == 0
    assert median_line_height([_line("a", height=18)]) == 18
    lines = [_line("a", height=10), _line("b", height=40), _line("c", height=12)]
    assert median_line_height(lines) == 12
    # Even count: the upper-middle element, same convention as group_blocks.
    assert median_line_height([_line("a", height=10), _line("b", height=20)]) == 20


# ---------------------------------------------------------- stabilize_lines


def _line(text: str, left: int = 100, top: int = 100, width: int = 200, height: int = 24):
    return OcrLine(text=text, left=left, top=top, width=width, height=height)


def _stab(previous, fresh, state=None, now=100.0, blank_check=None, scene_reset=False):
    return stabilize_lines(
        previous,
        fresh,
        state if state is not None else StabilizeState(),
        now=now,
        blank_check=blank_check,
        scene_reset=scene_reset,
    )


def test_stabilize_keeps_inspect_over_one_shot_misread():
    # Real-game case: an animated background made OCR read "nspect" for one
    # frame; the previously seen "Inspect" must hold, and small box jitter
    # must NOT repaint it (old geometry wins too).
    previous = [_line("Inspect")]
    fresh = [_line("nspect", left=104, top=101, width=188)]
    merged, state = _stab(previous, fresh)
    assert [line.text for line in merged] == ["Inspect"]
    # A partial read of the same word holds the settled geometry as well:
    # the fragment's narrower box must not shrink the cover.
    assert (merged[0].left, merged[0].top) == (100, 100)
    assert list(state.votes.values()) == [("nspect", 1, 100.0)]


def test_stabilize_upgrades_to_longer_reading_instantly():
    merged, state = _stab([_line("nspect")], [_line("Inspect")])
    assert [line.text for line in merged] == ["Inspect"]
    assert state.votes == {}


def test_stabilize_longer_garbage_reading_does_not_win_instantly():
    # Video noise inserts glyphs ("wOork"): more letters, but not a superset
    # of the settled text, so it must go through the votes like any change.
    merged, state = _stab([_line("I was at work.")], [_line("I was at wOork.")])
    assert merged[0].text == "I was at work."


def test_stabilize_adopts_consistent_new_reading_after_votes_and_time():
    merged, state = _stab([_line("Open door")], [_line("Shut door")], now=100.0)
    assert merged[0].text == "Open door"
    merged, state = _stab(merged, [_line("Shut door")], state, now=100.1)
    assert merged[0].text == "Open door"
    # Third identical reading but only 0.2 s in: still held (video passes
    # come 10x a second and three identical garbage reads do happen).
    merged, state = _stab(merged, [_line("Shut door")], state, now=100.2)
    assert merged[0].text == "Open door"
    merged, state = _stab(merged, [_line("Shut door")], state, now=100.5)
    assert merged[0].text == "Shut door"
    assert state.votes == {}  # the adopted change leaves no pending vote


def test_stabilize_resets_votes_on_inconsistent_candidates():
    merged, state = _stab([_line("Open door")], [_line("Shut door")], now=100.0)
    merged, state = _stab(merged, [_line("Shot door")], state, now=100.2)
    assert merged[0].text == "Open door"
    assert list(state.votes.values()) == [("shot door", 1, 100.2)]


def test_reading_score_prefers_clean_words():
    assert reading_score("Where were you last night?") == 1.0
    assert reading_score("Where wereyou]astunight?") <= 0.5
    assert reading_score("I was at wOork.") < 1.0
    assert reading_score("FIND PEOPLE TO PLAY WITH") == 1.0
    assert reading_score("") == 0.0


def test_stabilize_birth_adopts_the_cleaner_of_its_readings():
    good = OcrLine(text="Where were you last night?", left=730, top=548, width=458, height=35)
    garbled = OcrLine(text="Where wereyou]astunight?", left=731, top=548, width=457, height=35)
    merged, state = _stab([], [good], now=100.0)
    assert merged == []
    merged, state = _stab([], [garbled], state, now=100.1)
    assert [line.text for line in merged] == ["Where were you last night?"]
    assert (merged[0].left, merged[0].width) == (731, 457)  # geometry of the fresh read


def test_stabilize_partial_read_holds_the_whole_line_and_spawns_nothing():
    whole = OcrLine(
        text="Don't move. Stay right where you are.", left=600, top=900, width=700, height=36
    )
    # One pass reads the line in two pieces: the bigger piece pairs with the
    # whole line, the other piece must neither replace it nor be born.
    piece_a = OcrLine(text="Stay right where you are.", left=830, top=901, width=470, height=35)
    piece_b = OcrLine(text="Don't move.", left=601, top=901, width=200, height=35)
    merged, state = _stab([whole], [piece_a, piece_b], now=100.0)
    assert [line.text for line in merged] == ["Don't move. Stay right where you are."]
    assert state.births == {}
    # The same partial read persisting (three passes over > 0.35 s) is real.
    merged, state = _stab(merged, [piece_a], state, now=100.2)
    merged, state = _stab(merged, [piece_a], state, now=100.5)
    assert [line.text for line in merged] == ["Stay right where you are."]


def test_stabilize_garbled_reading_does_not_replace_a_clean_line():
    clean = _line("We have to leave before they find us.")
    garbled = _line("Weha e e to wave e o e they find s.")
    merged, state = _stab([clean], [garbled], now=100.0)
    assert merged == [clean]
    # A genuinely new, cleanly read sentence still replaces it at once.
    fresh = _line("Sarah, get the car started.")
    merged, state = _stab(merged, [fresh], state, now=100.1)
    assert merged[0].text == "Sarah, get the car started."


def test_stabilize_sudden_tilt_of_unchanged_text_is_held():
    # The engine sometimes mis-estimates skew on a busy video frame: every
    # line of that pass comes back tilted. Unchanged text must keep its
    # upright geometry until the tilt persists for a while.
    upright = _line("Where were you last night?")
    tilted = OcrLine(
        text="Where were you last night?",
        left=upright.left,
        top=upright.top,
        width=upright.width,
        height=upright.height,
        angle_deg=14.0,
    )
    merged, state = _stab([upright], [tilted], now=100.0)
    assert merged == [upright]
    merged, state = _stab(merged, [tilted], state, now=100.1)
    assert merged == [upright]
    merged, state = _stab(merged, [tilted], state, now=100.2)
    assert merged == [upright]
    merged, state = _stab(merged, [tilted], state, now=100.6)
    assert merged[0].angle_deg == 14.0  # a persistent tilt is real


def test_stabilize_jitter_holds_geometry_but_material_move_tracks():
    previous = [_line("Settings and privacy")]
    # Small jitter: text and geometry both hold.
    merged, state = _stab(previous, [_line("Settings and privacy.", left=103, top=102)])
    assert merged[0].text == "Settings and privacy"
    assert (merged[0].left, merged[0].top) == (100, 100)
    # Material move (>6 px, still overlapping enough to pair): new geometry
    # is adopted while the text stays the old one.
    merged, state = _stab(merged, [_line("Settings and privacy.", left=160, top=104)], state)
    assert merged[0].text == "Settings and privacy"
    assert (merged[0].left, merged[0].top) == (160, 104)


def test_stabilize_new_line_needs_a_confirming_second_sighting():
    # One-pass texture junk must never reach the screen: a brand-new line is
    # held on first sight and adopted on the second consecutive sighting.
    newcomer = _line("Fresh content", top=400)
    merged, state = _stab([], [newcomer])
    assert merged == []
    # The birth entry carries the held LINE so the app can force a confirming
    # re-OCR pass over its box even when the screen stays perfectly static.
    assert state.births == {(newcomer.left // 8, newcomer.top // 8): (1, newcomer)}
    merged, state = _stab([], [newcomer], state)
    assert merged == [newcomer]
    assert state.births == {}


def test_stabilize_one_pass_junk_never_shows():
    merged, state = _stab([], [_line("eoo t", top=300)])
    assert merged == []
    merged, state = _stab([], [], state)  # junk vanished: birth entry pruned
    assert merged == []
    assert state.births == {}


def test_stabilize_scene_reset_adopts_everything_instantly():
    old_state = StabilizeState(
        votes={(1, 1): ("stale", 2, 0.0)},
        misses={(2, 2): (1, 90.0)},
        births={(3, 3): (1, _line("held"))},
    )
    fresh = [_line("New page title"), _line("New page body", top=200)]
    merged, state = _stab([_line("Old page")], fresh, old_state, scene_reset=True)
    assert merged == fresh  # no debounce on a page switch
    assert (state.votes, state.misses, state.births) == ({}, {}, {})


def test_stabilize_adopts_genuinely_different_text_at_once():
    merged, _state = _stab([_line("Inventory")], [_line("Settings")])
    assert merged[0].text == "Settings"


def test_stabilize_treats_whitespace_variants_as_equal():
    merged, _state = _stab([_line("It's  a  new  night")], [_line("It's a new night")])
    assert merged[0].text == "It's  a  new  night"


def test_stabilize_pairs_lines_one_to_one_by_row():
    previous = [_line("First row of text", top=100), _line("Second row of text", top=200)]
    fresh = [_line("First row of text.", top=100), _line("Second row of text.", top=200)]
    merged, _state = _stab(previous, fresh)
    assert [line.text for line in merged] == ["First row of text", "Second row of text"]


# ------------------------------------------------------------ ghost line TTL


def test_stabilize_ghost_survives_fast_misses_dies_after_timed_ones():
    ghost = _line("It's a new night")
    key = (ghost.left // 8, ghost.top // 8)
    # Two rapid misses within the minimum age: the ghost must survive both.
    merged, state = _stab([ghost], [], now=100.0)
    assert merged == [ghost]
    assert state.misses == {key: (1, 100.0)}
    merged, state = _stab(merged, [], state, now=100.1)
    assert merged == [ghost]
    assert state.misses[key][0] == 2
    # Enough wall time elapsed AND repeated misses: now it dies.
    merged, state = _stab(merged, [], state, now=103.0)
    assert merged == []
    assert state.misses == {}


def test_stabilize_ghost_reappearing_resets_miss_counter():
    ghost = _line("Inspect")
    merged, state = _stab([ghost], [], now=100.0)
    assert state.misses != {}
    merged, state = _stab(merged, [_line("Inspect")], state, now=100.2)
    assert [line.text for line in merged] == ["Inspect"]
    assert state.misses == {}
    merged, state = _stab(merged, [], state, now=100.4)
    assert [line.text for line in merged] == ["Inspect"]
    assert state.misses[(12, 12)][0] == 1


def test_stabilize_blank_area_drops_ghost_immediately():
    merged, state = _stab([_line("Gone text")], [], blank_check=lambda line: True)
    assert merged == []
    assert state.misses == {}


def test_stabilize_blank_check_ignored_for_paired_lines():
    merged, state = _stab(
        [_line("Inspect")], [_line("Inspect", left=104)], blank_check=lambda line: True
    )
    assert [line.text for line in merged] == ["Inspect"]
    assert state.misses == {}


def test_stabilize_ghost_rides_along_while_new_content_confirms():
    previous = [_line("Old subtitle", top=100)]
    fresh = [_line("Fresh content", top=400)]
    merged, state = _stab(previous, fresh, now=100.0)
    # The newcomer is debounced (birth), the ghost is kept on its first miss.
    assert [line.text for line in merged] == ["Old subtitle"]
    merged, state = _stab(merged, fresh, state, now=100.2)
    assert sorted(line.text for line in merged) == ["Fresh content", "Old subtitle"]
