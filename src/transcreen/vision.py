"""Pure image/typography helpers for the live loop.

Kept free of tkinter so they are unit-testable and usable from the worker
thread without touching the UI.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import cast

from PIL import Image, ImageChops

from .ocr import OcrLine
from .screen import Rect
from .translator import looks_like_text

# Signature raster: 192x108 grayscale, split into 12px tiles -> 16x9 grid.
SIG_W = 192
SIG_H = 108
TILE = 12
_TILES_X = SIG_W // TILE
_TILES_Y = SIG_H // TILE

MOTION_THRESHOLD = 1.0  # global mean abs diff meaning "the screen is moving"
TILE_THRESHOLD = 6.0  # per-tile mean abs diff meaning "this tile changed"
DIRTY_PAD = 28  # px added around the dirty rect before OCR / line eviction

_CONTRAST_MIN = 90  # channel-sum distance below which fg gets a fallback color
_BOLD_INK_RATIO = 0.25  # measured: regular Segoe ~0.19, bold ~0.30

# Selection-reconcile: keep the previous line when the fresh OCR of its area
# is a >=50% similar corrupted variant (text selection garbles OCR badly).
_RECONCILE_SIMILARITY = 0.5

# Paragraph grouping tolerances. Calibrated down after live-game observation:
# looser bands glued a game HUD to an overlapping console window's text.
_BLOCK_LEFT_TOLERANCE = 28
_BLOCK_CENTER_TOLERANCE = 48  # subtitles/dialogs are center-aligned, not left
_BLOCK_GAP_FACTOR = 1.1
_BLOCK_HEIGHT_RATIO = (0.62, 1.6)

# Sentence continuation: a block whose last line has no terminal punctuation
# adopts a lowercase follower even when left/center alignment fails
# ("I didn't realise it was this" + "late.").
_CONTINUATION_GAP_FACTOR = 1.6
_CONTINUATION_X_OVERLAP = 0.5  # of the shorter width
_TERMINAL_PUNCTUATION = (".", "!", "?", "…", ":", ";")

# Continuation brakes: the open sentence must look like prose (>= 2 words, or
# a single word ending in a comma/letter), and a short Title Case / ALL CAPS
# follower is a UI label, never a wrapped sentence tail.
_CONTINUATION_MIN_PREV_WORDS = 2
_LABEL_MAX_WORDS = 2

# Same-row fragment merging (games/subtitles with wide word spacing). 1.8x
# line height: real word gaps stay below it, while separate UI fields
# (inventory labels, stat columns) sit further apart and must not be glued.
_ROW_GAP_FACTOR = 1.8
_ROW_CENTER_ALIGN = 0.5  # vertical-center diff limit, in units of line height
_DUPLICATE_OVERLAP = 0.5  # area overlap that makes two equal texts one line

# Menu brake: a vertical stack of short ALL CAPS / Title Case buttons ("PLAY",
# "SETTINGS", "CREDITS") is a list, never a paragraph. Measured on a live game
# menu, where grouping produced "KREDİ KREDİ AYARLARI".
_MENU_LABEL_MAX_WORDS = 3
_MENU_GAP_FACTOR = 0.8  # buttons sit at least this far apart, captions do not

# Stylized logos ("RISE of STARFALL") come back as fragments of wildly
# different heights; for big text the row merger tolerates that so the whole
# title becomes one line instead of half-covered.
_BIG_TEXT_HEIGHT = 60
_BIG_ROW_HEIGHT_RATIO = (0.35, 2.8)

# Classic OCR digit-for-letter confusions inside words ("ni9ht" -> "night").
_DIGIT_FIX_LOWER = {"0": "o", "1": "l", "3": "e", "5": "s", "9": "g"}
_DIGIT_FIX_UPPER = {"0": "O", "1": "I", "3": "E", "5": "S", "9": "G"}

# Stricter live-OCR text gate: inside a token that already contains a digit
# these letters are the classic letter-for-digit misreads, so "2OO / 2OO"
# normalizes to "200 / 200" and never reaches the translator.
_CONFUSABLE_LETTERS = set("OolISsBD")  # letters tolerated in a numeric token
_CONFUSABLE_TO_DIGIT = str.maketrans("OlISsB", "011558")  # o/D stay letters
_MIN_REAL_LETTERS = 3
_LETTER_RE = re.compile(r"[^\W\d_]")  # any-script letter (translator's rule)
_ICON_REPEAT_LIMIT = 3  # >= 3 identical single-letter tokens in a row = slots

# Turkish source detection: text already in the target language must never be
# translated or covered. A Turkish-specific letter is decisive on its own;
# otherwise >= 25% of the words (at least one) must be Turkish stopwords.
_TURKISH_SPECIFIC_CHARS = frozenset("çğıİöşüÇĞÖŞÜ")
_TURKISH_STOPWORDS = frozenset(
    {
        "ve", "bir", "bu", "şu", "için", "ile", "çok", "daha", "gibi",
        "kadar", "sonra", "önce", "değil", "var", "yok", "ama", "evet",
        "hayır", "ben", "sen", "biz", "siz", "ne", "nasıl", "neden", "tamam",
    }
)  # fmt: skip
_TURKISH_STOPWORD_RATIO = 0.25
_TR_CASEFOLD_TABLE = str.maketrans({"I": "ı", "İ": "i"})  # Turkish dotted/dotless I
_WORD_RE = re.compile(r"[^\W\d_]+")

# Quantity affixes around item names ("x2 BANDAGES", "Ammo 30"): peeled off
# before translation so the number survives verbatim and the cache keys on
# the core text alone.
_QUANTITY_TOKEN = r"[xX]\d+|\d+[xX]|\d+"
_QUANTITY_PREFIX_RE = re.compile(rf"^(?:(?:{_QUANTITY_TOKEN})\s+)+")
_QUANTITY_SUFFIX_RE = re.compile(rf"(?:\s+(?:{_QUANTITY_TOKEN}))+$")

# Cross-frame stabilization: animated backgrounds make OCR flicker between
# readings of unchanged on-screen text.
_STABLE_PAIR_V_OVERLAP = 0.5  # of the shorter height
_STABLE_PAIR_H_OVERLAP = 0.4  # of the shorter width
_STABLE_JITTER_RATIO = 0.93  # >=: same text modulo jitter -> keep the old
_STABLE_CHANGE_RATIO = 0.55  # <: genuinely different text -> adopt at once
_STABLE_VOTES_NEEDED = 3  # consistent readings before a mid-band change wins
# ...spread over at least this long: at ~10 passes/s over video, three
# identical garbage readings do happen; identical for a third of a second
# they do not. The same vote gate guards against a sudden tilt of unchanged
# text (the engine mis-estimating skew on a busy frame).
_STABLE_VOTE_MIN_S = 0.35
_ANGLE_VOTE_DEG = 6.0
_SCORE_DROP_MAX = 0.3  # a reading this much less word-like than the old one is noise
_VOTE_BUCKET_PX = 8  # vote keys quantize the old line's position
_GHOST_MISS_LIMIT = 2  # consecutive OCR misses before an unpaired line dies
_GHOST_MIN_AGE_S = 2.5  # ...and at least this much wall time must have passed
# (measured: an animated background can starve one menu label of OCR hits for
# well over a second, and a 1.2 s TTL dropped covers that were still needed)
_BIRTH_CONFIRM = 2  # consecutive sightings before a brand-new line is drawn
# A held birth is re-sighted when a fresh line overlaps it with this much text
# similarity: boxes over video jitter by more than the 8 px vote bucket, and
# an exact-bucket match kept restarting the count (subtitles never confirmed).
_BIRTH_MATCH_RATIO = 0.6

# Letters outside this set (ø, æ, ...) almost never appear in the languages on
# this screen; a text whose letters are mostly exotic is texture noise.
_FAMILIAR_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZçğıİöşüÇĞÖŞÜ")
_FAMILIAR_MIN_RATIO = 0.6
# Two-letter utterances that are real subtitles/labels, not fragments: the
# three-letter floor would otherwise drop "No!" and "OK" for good.
_SHORT_WORDS = frozenset(
    {
        "no", "go", "hi", "ok", "oh", "ah", "um", "uh", "hm", "eh", "ow", "ha", "aw", "ew",
        "yo", "ya", "me", "my", "we", "us", "up", "so", "on", "in", "it", "is", "do", "be",
    }
)  # fmt: skip
_VOWELS = frozenset("aeıioöuüâîûAEIİOÖUÜ")
# ink_fraction: a pixel this close (luminance-weighted channel distance) to
# the text color counts as text ink.
_INK_TOLERANCE = 60

Votes = dict[tuple[int, int], tuple[str, int, float]]  # candidate, count, first seen
Misses = dict[tuple[int, int], int]

# Segoe UI ink metrics relative to em size: caps/ascenders ~0.72, x-height
# ~0.50, descender depth ~0.23. Digits reach cap height; i carries a tittle.
_ASCENDER_CHARS = set("bdfhklti0123456789")
_DESCENDER_CHARS = set("gjpqy")


def frame_signature(img: Image.Image) -> bytes:
    """Tiny grayscale fingerprint of a frame for cheap change detection."""
    return img.resize((SIG_W, SIG_H), Image.Resampling.BILINEAR).convert("L").tobytes()


def signatures_differ(
    a: bytes | None, b: bytes | None, threshold: float = MOTION_THRESHOLD
) -> bool:
    if a is None or b is None or len(a) != len(b):
        return True
    if a == b:  # C-speed equality: the common static-screen case
        return False
    total = 0
    for x, y in zip(a, b, strict=True):
        total += abs(x - y)
    return total / len(a) > threshold


def dirty_rect(
    prev: bytes | None, curr: bytes, frame_width: int, frame_height: int
) -> tuple[Rect | None, float]:
    """Bounding box of changed tiles between two signatures, in frame pixels.

    Returns (None, 0.0) when nothing changed, and (full frame, 1.0) when there
    is no previous signature to compare against. The float is the fraction of
    tiles that changed, so callers can fall back to full-frame OCR.
    """
    full = Rect(0, 0, frame_width, frame_height)
    if prev is None or len(prev) != len(curr):
        return full, 1.0
    if prev == curr:  # C-speed equality: skip the tile scan entirely
        return None, 0.0

    changed: list[tuple[int, int]] = []
    tile_area = TILE * TILE
    for ty in range(_TILES_Y):
        for tx in range(_TILES_X):
            total = 0
            for row in range(TILE):
                base = (ty * TILE + row) * SIG_W + tx * TILE
                for offset in range(TILE):
                    total += abs(curr[base + offset] - prev[base + offset])
            if total / tile_area > TILE_THRESHOLD:
                changed.append((tx, ty))
    if not changed:
        return None, 0.0

    fraction = len(changed) / (_TILES_X * _TILES_Y)
    scale_x = frame_width / SIG_W
    scale_y = frame_height / SIG_H
    min_tx = min(t[0] for t in changed)
    max_tx = max(t[0] for t in changed)
    min_ty = min(t[1] for t in changed)
    max_ty = max(t[1] for t in changed)
    left = max(0, int(min_tx * TILE * scale_x) - DIRTY_PAD)
    top = max(0, int(min_ty * TILE * scale_y) - DIRTY_PAD)
    right = min(frame_width, int((max_tx + 1) * TILE * scale_x) + DIRTY_PAD)
    bottom = min(frame_height, int((max_ty + 1) * TILE * scale_y) + DIRTY_PAD)
    return Rect(left, top, right - left, bottom - top), fraction


def font_px_for_line(text: str, box_height: int) -> int:
    """Em size whose rendered ink height matches the OCR box height.

    OCR boxes hug the ink, so the right em size depends on which glyph zones
    the text occupies (validated against real Windows OCR boxes: 30px Segoe UI
    "Settings" yields a 29px box -> factor 0.95).
    """
    has_ascender = any(c.isupper() or c in _ASCENDER_CHARS for c in text)
    has_descender = any(c in _DESCENDER_CHARS for c in text)
    if has_ascender and has_descender:
        factor = 0.95
    elif has_ascender:
        factor = 0.72
    elif has_descender:
        factor = 0.73
    else:
        factor = 0.52
    return max(8, min(240, round(box_height / factor)))


def pick_colors(frame: Image.Image, line: OcrLine) -> tuple[str, str, bool]:
    """Estimate (background, text, is_bold) for a text line from the frame.

    Background = per-channel median of the box's border pixels; text color =
    the sampled pixel farthest from that background, with a black/white
    fallback when contrast is too low to trust the estimate. Boldness = the
    fraction of pixels on the text side of the gray midpoint (measured:
    regular ~0.19, bold ~0.30).
    """
    pad = 3
    left = max(0, line.left - pad)
    top = max(0, line.top - pad)
    right = min(frame.width, line.left + line.width + pad)
    bottom = min(frame.height, line.top + line.height + pad)
    if right <= left or bottom <= top:
        return "#202020", "#f0f0f0", False

    crop = frame.crop((left, top, right, bottom)).convert("RGB")
    # NEAREST keeps genuine pixel colors instead of blending glyph and bg.
    sample = crop.resize((20, 10), Image.Resampling.NEAREST)
    pixels = cast("list[tuple[int, int, int]]", list(sample.get_flattened_data()))
    border = (
        pixels[:20]
        + pixels[180:200]
        + [pixels[row * 20] for row in range(10)]
        + [pixels[row * 20 + 19] for row in range(10)]
    )
    bg = tuple(sorted(p[i] for p in border)[len(border) // 2] for i in range(3))
    fg = max(pixels, key=lambda p: _dist(p, bg))
    low_contrast = _dist(fg, bg) < _CONTRAST_MIN
    if low_contrast:
        fg = (16, 16, 16) if _luminance(bg) > 140 else (240, 240, 240)

    bold = False
    if not low_contrast:
        # Density over the TIGHT ink box (the padded crop dilutes the ratio;
        # thresholds were measured on tight boxes: regular ~0.19, bold ~0.30).
        tight = frame.crop(
            (
                max(0, line.left),
                max(0, line.top),
                min(frame.width, line.left + line.width),
                min(frame.height, line.top + line.height),
            )
        ).convert("L")
        histogram = tight.histogram()
        total = sum(histogram) or 1
        midpoint = int((_luminance(bg) + _luminance(fg)) / 2)
        midpoint = min(255, max(0, midpoint))
        if _luminance(fg) < _luminance(bg):  # dark text on light bg
            ink = sum(histogram[: midpoint + 1])
        else:
            ink = sum(histogram[midpoint:])
        bold = ink / total > _BOLD_INK_RATIO
    return _hex(bg), _hex(fg), bold


def dedupe_overlapping_lines(lines: list[OcrLine]) -> list[OcrLine]:
    """Drop repeated detections of the same text at the same place.

    Live-menu measurement: the engine sometimes reports one visual line twice
    with slightly different boxes. They land on different "rows", so row
    merging cannot see them, and block grouping then glues them into
    "SETTINGS SETTINGS" -> "AYARLAR AYARLARI". Two lines with equal
    normalized text whose boxes overlap by at least half of the smaller area
    are the same line; the larger box survives.
    """
    survivors: list[OcrLine] = []
    for line in sorted(lines, key=lambda item: -(item.width * item.height)):
        text = _norm_text(line.text)
        duplicate = False
        for kept in survivors:
            if _norm_text(kept.text) != text:
                continue
            overlap_w = _axis_overlap_fraction(kept.left, kept.width, line.left, line.width)
            overlap_h = _axis_overlap_fraction(kept.top, kept.height, line.top, line.height)
            if overlap_w * overlap_h >= _DUPLICATE_OVERLAP:
                duplicate = True
                break
        if not duplicate:
            survivors.append(line)
    return sorted(survivors, key=lambda item: (item.top, item.left))


def merge_row_fragments(lines: list[OcrLine]) -> list[OcrLine]:
    """Merge same-baseline fragments that wide word spacing split apart.

    Game/subtitle text with large word gaps comes back from OCR as several
    "lines" on one visual row ("It's | a new night. | Read | the note");
    translating them separately produces word salad. Fragments whose vertical
    centers align and whose horizontal gap stays below ~1.8x the line height
    are re-joined into one line before grouping and translation; wider gaps
    are separate UI fields and stay apart.
    """
    upright = sorted(
        (line for line in lines if line.angle_deg == 0.0),
        key=lambda item: (item.top, item.left),
    )
    rows: list[list[OcrLine]] = []
    for line in upright:
        center_y = line.top + line.height / 2
        placed = False
        for row in rows:
            last = row[-1]
            last_center = last.top + last.height / 2
            ref_h = max(last.height, line.height, 1)
            gap = line.left - (last.left + last.width)
            ratio = line.height / max(last.height, 1)
            big = min(last.height, line.height) >= _BIG_TEXT_HEIGHT
            low, high = _BIG_ROW_HEIGHT_RATIO if big else (0.55, 1.8)
            if (
                abs(center_y - last_center) <= ref_h * _ROW_CENTER_ALIGN
                and low <= ratio <= high
                and -8 <= gap <= ref_h * _ROW_GAP_FACTOR
            ):
                row.append(line)
                placed = True
                break
        if not placed:
            rows.append([line])

    merged: list[OcrLine] = []
    for row in rows:
        # Band-seam duplicates: parallel OCR occasionally emits the same text
        # twice side by side ("TODAY'S REVENUE TODAY'S REVENUE"); drop a
        # fragment that repeats its predecessor's text on the same row.
        deduped: list[OcrLine] = []
        for fragment in row:
            if deduped and _norm_text(fragment.text) == _norm_text(deduped[-1].text):
                continue
            deduped.append(fragment)
        row = deduped
        if len(row) == 1:
            merged.append(row[0])
            continue
        left = min(line.left for line in row)
        top = min(line.top for line in row)
        right = max(line.left + line.width for line in row)
        bottom = max(line.top + line.height for line in row)
        inks = sorted((line.ink_height or line.height) for line in row)
        merged.append(
            OcrLine(
                text=" ".join(line.text for line in row),
                left=left,
                top=top,
                width=right - left,
                height=bottom - top,
                ink_height=inks[len(inks) // 2],
                run_length=right - left,
                anchor_x=left,
                anchor_y=top + (bottom - top) // 2,
            )
        )
    merged.extend(line for line in lines if line.angle_deg != 0.0)
    return merged


def fix_ocr_digits(text: str) -> str:
    """Repair digit-for-letter OCR confusions inside words ("ni9ht" -> "night").

    Only digits flanked by letters on BOTH sides are replaced, so genuine
    alphanumerics (v2, R2D2, Windows 11) stay untouched.
    """
    chars = list(text)
    for i in range(1, len(chars) - 1):
        char = chars[i]
        if char in _DIGIT_FIX_LOWER and chars[i - 1].isalpha() and chars[i + 1].isalpha():
            upper = chars[i - 1].isupper() and chars[i + 1].isupper()
            chars[i] = _DIGIT_FIX_UPPER[char] if upper else _DIGIT_FIX_LOWER[char]
    return "".join(chars)


def _normalize_numeric_lookalikes(text: str) -> str:
    """Map letter-for-digit OCR misreads to digits inside numeric tokens.

    Within each whitespace token that contains at least one digit and whose
    letters ALL come from the confusable set, the classic lookalikes O->0,
    l/I->1, S/s->5, B->8 become digits ("2OO" -> "200"). A token with any
    other letter (v2, x2, HP20) is genuine alphanumerics and stays untouched.
    """
    tokens = text.split()
    for i, token in enumerate(tokens):
        letters = [c for c in token if c.isalpha()]
        if (
            letters
            and any(c.isdigit() for c in token)
            and all(c in _CONFUSABLE_LETTERS for c in letters)
        ):
            tokens[i] = token.translate(_CONFUSABLE_TO_DIGIT)
    return " ".join(tokens)


def _is_icon_junk(text: str) -> bool:
    """Detect letter salad OCR'd from icon rows and empty inventory slots.

    A row of round/narrow icons reads as short confusable-only tokens
    ("O O O", "ll OO"), and >= 3 consecutive repeats of the same
    single-letter token are a slot grid even off the confusable set
    ("A A A"). Real short words ("Yes", "İki kelime") contain a 3+ letter
    token or non-confusable letters and pass untouched.
    """
    tokens = text.split()
    alnum_tokens = [t for t in tokens if any(c.isalnum() for c in t)]
    letters = [c for t in alnum_tokens for c in t if c.isalpha()]
    if (
        letters
        and all(sum(c.isalpha() for c in t) <= 2 for t in alnum_tokens)
        and all(c in _CONFUSABLE_LETTERS for c in letters)
    ):
        return True
    run, prev = 0, ""
    for token in tokens:
        if len(token) == 1 and token.isalpha():
            key = token.casefold()
            run = run + 1 if key == prev else 1
            prev = key
            if run >= _ICON_REPEAT_LIMIT:
                return True
        else:
            run, prev = 0, ""
    return False


def _is_short_word(text: str) -> bool:
    """A lone two-letter word from the whitelist ("No!", "OK", "Hi.")."""
    tokens = _WORD_RE.findall(text)
    return len(tokens) == 1 and tokens[0].lower() in _SHORT_WORDS


def is_probable_word(text: str) -> bool:
    """Short line that is real language rather than an icon misread.

    Used to exempt big short subtitles ("Yes.", "Run!", "GO") from the
    oversized-junk guard, which exists for icon boxes read as "HP" or "eøø".
    Sentence punctuation, a whitelisted short word, or a single familiar
    token with a vowel all qualify; vowelless or exotic salad does not.
    """
    stripped = text.strip()
    if stripped.endswith(_TERMINAL_PUNCTUATION):
        return True
    tokens = _WORD_RE.findall(stripped)
    if len(tokens) != 1:
        return False
    word = tokens[0]
    if word.lower() in _SHORT_WORDS:
        return True
    return (
        len(word) >= 3
        and any(c in _VOWELS for c in word)
        and all(c in _FAMILIAR_LETTERS for c in word)
    )


def is_real_text(text: str) -> bool:
    """Stricter OCR-line gate than translator.looks_like_text.

    Numeric lookalikes are normalized first, so a game counter OCR'd as
    "2OO / 2OO" is recognized as "200 / 200" and filtered instead of being
    translated into nonsense. At least three letters must remain — tiny
    fragments like "I.E." lose their translation but stop rendering as giant
    junk — icon/slot letter salad ("O O O") is rejected, and the
    letters-majority and vowel rules of looks_like_text still apply to the
    normalized text.
    """
    normalized = _normalize_numeric_lookalikes(text)
    letters = _LETTER_RE.findall(normalized)
    if len(letters) < _MIN_REAL_LETTERS and not _is_short_word(normalized):
        return False
    # Texture noise reads as exotic glyph salad ("eøø t"); real English or
    # Turkish text is made of familiar letters.
    familiar = sum(1 for letter in letters if letter in _FAMILIAR_LETTERS)
    if familiar / len(letters) < _FAMILIAR_MIN_RATIO:
        return False
    if _is_icon_junk(normalized):
        return False
    return looks_like_text(normalized)


def looks_turkish(text: str) -> bool:
    """True when the text is already Turkish and must skip translation.

    A Turkish-specific letter (çğıİöşüÇĞÖŞÜ) is decisive on its own;
    plain-ASCII Turkish is caught by its function words: at least TWO of the
    words, making up a quarter of the text, must be Turkish stopwords. Two
    hits, not one: English collides with the set on single words ("Ben walks
    in", "var x = 1") and a one-hit rule silently skipped such lines. Words
    are lowercased with Turkish I-mapping so "HAYIR" matches "hayır".
    """
    if any(c in _TURKISH_SPECIFIC_CHARS for c in text):
        return True
    words = _WORD_RE.findall(text.translate(_TR_CASEFOLD_TABLE).lower())
    if not words:
        return False
    hits = sum(1 for word in words if word in _TURKISH_STOPWORDS)
    return hits >= 2 and hits / len(words) >= _TURKISH_STOPWORD_RATIO


def split_quantity_affixes(text: str) -> tuple[str, str, str]:
    """Split leading/trailing quantity tokens off a line: (prefix, core, suffix).

    "x2 BANDAGES" -> ("x2 ", "BANDAGES", ""); "Ammo 30" -> ("", "Ammo", " 30").
    Only the core should be translated (and cached); the affixes are
    re-attached verbatim at draw time, so "x2" can never be garbled into
    "x?". Affix tokens keep the exact whitespace that separated them, the
    core keeps everything else verbatim, and prefix + core + suffix always
    reassembles the original text. Text that would leave an empty core is
    returned whole as the core.
    """
    prefix_match = _QUANTITY_PREFIX_RE.match(text)
    prefix = prefix_match.group(0) if prefix_match else ""
    rest = text[len(prefix) :]
    suffix_match = _QUANTITY_SUFFIX_RE.search(rest)
    suffix = suffix_match.group(0) if suffix_match else ""
    core = rest[: len(rest) - len(suffix)]
    if not core.strip():
        return "", text, ""
    return prefix, core, suffix


def median_line_height(lines: list[OcrLine]) -> int:
    """Median box height of the given lines, 0 when there are none.

    The app's oversized-junk guard compares a candidate box against the
    screen's median line height to reject huge icon misreads.
    """
    if not lines:
        return 0
    heights = sorted(line.height for line in lines)
    return heights[len(heights) // 2]


def group_blocks(lines: list[OcrLine]) -> list[list[OcrLine]]:
    """Group upright lines into paragraph blocks.

    Consecutive lines with matching left edges OR matching centers (subtitles
    and dialog boxes are center-aligned), similar heights and normal line
    spacing translate together as one unit, which gives the translator full
    sentence context. A lowercase line under a block whose sentence is still
    open joins as a continuation even when alignment fails ("I didn't realise
    it was this" + "late."), with a slightly wider gap allowance. Rotated
    lines stay as single-line blocks.
    """
    upright = sorted(
        (line for line in lines if line.angle_deg == 0.0),
        key=lambda item: (item.top, item.left),
    )
    blocks: list[list[OcrLine]] = []
    for line in upright:
        placed = False
        line_center = line.left + line.width / 2
        for block in blocks:
            last = block[-1]
            heights = sorted(x.height for x in block)
            median_h = heights[len(heights) // 2]
            gap = line.top - (last.top + last.height)
            low, high = _BLOCK_HEIGHT_RATIO
            block_center = block[0].left + block[0].width / 2
            aligned = (
                abs(line.left - block[0].left) <= _BLOCK_LEFT_TOLERANCE
                or abs(line_center - block_center) <= _BLOCK_CENTER_TOLERANCE
            )
            joins = aligned and gap <= int(median_h * _BLOCK_GAP_FACTOR)
            if (
                joins
                and gap >= median_h * _MENU_GAP_FACTOR
                and _is_menu_label(last)
                and _is_menu_label(line)
            ):
                # A stack of buttons is a list, not a paragraph — but only
                # when they are spaced apart like menu entries. Tightly set
                # caps lines are a wrapped caption ("FIND PEOPLE" / "TO PLAY
                # WITH") and must stay one sentence.
                joins = False
            if not joins and gap <= median_h * _CONTINUATION_GAP_FACTOR:
                joins = _is_continuation(last, line)
            if joins and gap >= -4 and median_h > 0 and low <= line.height / median_h <= high:
                block.append(line)
                placed = True
                break
        if not placed:
            blocks.append([line])
    blocks.extend([line] for line in lines if line.angle_deg != 0.0)
    return blocks


def _is_menu_label(line: OcrLine) -> bool:
    """True for a short standalone UI button caption ("PLAY", "Settings")."""
    text = line.text.strip()
    if not text or text.endswith(_TERMINAL_PUNCTUATION):
        return False
    words = text.split()
    if len(words) > _MENU_LABEL_MAX_WORDS:
        return False
    return text.isupper() or all(word[:1].isupper() for word in words if word[:1].isalpha())


def _is_continuation(last: OcrLine, line: OcrLine) -> bool:
    """True when `line` reads like the wrapped tail of `last`'s sentence.

    An unfinished sentence ("I didn't realise it was this") followed by a
    short lowercase line ("late.") belongs to the same dialog block even when
    left/center alignment fails; requiring overlapping x-ranges keeps real
    columns apart. Brakes against gluing non-sentences: the open line must
    look like prose (>= 2 words, or a single word ending in a comma or
    letter — a lone "42" or "+" adopts nothing), and a short Title Case /
    ALL CAPS follower is a UI label, never a sentence tail.
    """
    prev_text = last.text.rstrip()
    if not prev_text or prev_text.endswith(_TERMINAL_PUNCTUATION):
        return False
    if not line.text.lstrip()[:1].islower():
        return False
    if len(prev_text.split()) < _CONTINUATION_MIN_PREV_WORDS and not (
        prev_text.endswith(",") or bool(_LETTER_RE.fullmatch(prev_text[-1]))
    ):
        return False
    if _is_short_label(line.text):
        return False
    overlap = _axis_overlap_fraction(last.left, last.width, line.left, line.width)
    return overlap >= _CONTINUATION_X_OVERLAP


def _is_short_label(text: str) -> bool:
    """<= 2-word Title Case or ALL CAPS text is a UI label, not sentence prose."""
    stripped = text.strip()
    words = stripped.split()
    if not words or len(words) > _LABEL_MAX_WORDS:
        return False
    return stripped.isupper() or stripped.istitle()


def reconcile_selection(previous: list[OcrLine], fresh: list[OcrLine]) -> list[OcrLine]:
    """Keep previous lines whose fresh re-OCR is a garbled selection variant.

    A text-selection highlight fragments and corrupts OCR ("jumps over" ->
    "•um s over"). When the fresh lines overlapping an old line read >=50%
    similar to it, the old line (with its cached translation) wins and the
    corrupted fragments are dropped.
    """
    consumed: set[int] = set()
    result: list[OcrLine] = []
    for old in previous:
        overlapping = [
            i
            for i, line in enumerate(fresh)
            if i not in consumed
            and old.top - 4 <= line.top + line.height / 2 <= old.top + old.height + 4
            and line.left < old.left + old.width
            and old.left < line.left + line.width
        ]
        if overlapping:
            combined = " ".join(fresh[i].text for i in overlapping)
            ratio = SequenceMatcher(None, old.text.lower(), combined.lower()).ratio()
            if ratio >= _RECONCILE_SIMILARITY:
                result.append(old)
                consumed.update(overlapping)
    result.extend(line for i, line in enumerate(fresh) if i not in consumed)
    return result


@dataclass
class StabilizeState:
    """Cross-frame damping state carried by the live loop between OCR passes.

    votes: mid-similarity change candidates (old text holds until the same
    reading repeats). misses: ghost TTL entries as (count, first_miss_time).
    births: brand-new lines awaiting a confirming second sighting.
    """

    votes: Votes = field(default_factory=dict)
    misses: dict[tuple[int, int], tuple[int, float]] = field(default_factory=dict)
    # Birth entries carry the held LINE, not just a counter: on a static
    # screen its area never goes dirty again, so the app must be able to
    # force a confirming re-OCR pass over exactly that box.
    births: dict[tuple[int, int], tuple[int, OcrLine]] = field(default_factory=dict)


def stabilize_lines(
    previous: list[OcrLine],
    fresh: list[OcrLine],
    state: StabilizeState,
    *,
    now: float,
    blank_check: Callable[[OcrLine], bool] | None = None,
    scene_reset: bool = False,
) -> tuple[list[OcrLine], StabilizeState]:
    """Damp cross-frame OCR flicker: keep stable text, adopt real changes fast.

    Live-game measurement showed the dominant instability classes: one-pass
    junk lines blinking in and out (92% of churn events were gaps), size/angle
    jitter on stable text, and stale pages surviving a scene switch. Rules:

    * ``scene_reset`` (a page switch): adopt ``fresh`` wholesale and clear all
      damping state — the new screen must appear instantly and old votes or
      ghosts must not leak onto it.
    * Pairing: fresh lines pair one-to-one with previous lines by box overlap
      (>=50% of the shorter height, >=40% of the shorter width), greedily by
      best text similarity; _stabilize_pair picks the surviving text and,
      for unchanged text, also holds the old geometry unless it materially
      moved/resized (kills font-size and angle flapping).
    * Births: an unpaired fresh line is NOT drawn on first sight; it must be
      seen on ``_BIRTH_CONFIRM`` consecutive passes (one-pass texture junk
      never reaches the screen). Held entries live in ``state.births``.
    * Ghosts: an unpaired previous line whose area went blank
      (``blank_check``) dies immediately; otherwise it dies only after
      ``_GHOST_MISS_LIMIT`` consecutive misses AND ``_GHOST_MIN_AGE_S`` of
      wall time (rapid motion passes cannot flicker it away).
    """
    if scene_reset:
        return list(fresh), StabilizeState()

    pairs: list[tuple[float, int, int]] = []
    for prev_idx, old in enumerate(previous):
        for fresh_idx, new in enumerate(fresh):
            v_overlap = _axis_overlap_fraction(old.top, old.height, new.top, new.height)
            h_overlap = _axis_overlap_fraction(old.left, old.width, new.left, new.width)
            if v_overlap >= _STABLE_PAIR_V_OVERLAP and h_overlap >= _STABLE_PAIR_H_OVERLAP:
                ratio = SequenceMatcher(None, old.text.casefold(), new.text.casefold()).ratio()
                pairs.append((ratio, prev_idx, fresh_idx))
    pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
    matched: dict[int, tuple[int, float]] = {}  # fresh idx -> (previous idx, ratio)
    used_previous: set[int] = set()
    for ratio, prev_idx, fresh_idx in pairs:
        if prev_idx in used_previous or fresh_idx in matched:
            continue
        used_previous.add(prev_idx)
        matched[fresh_idx] = (prev_idx, ratio)

    merged: list[OcrLine] = []
    kept = StabilizeState()
    for fresh_idx, new in enumerate(fresh):
        if fresh_idx in matched:
            prev_idx, ratio = matched[fresh_idx]
            merged.append(
                _stabilize_pair(previous[prev_idx], new, ratio, state.votes, kept.votes, now)
            )
            continue
        if _fragment_of_tracked_line(previous, used_previous, new):
            continue  # a piece of a line that is still tracked whole: not new content
        key = (new.left // _VOTE_BUCKET_PX, new.top // _VOTE_BUCKET_PX)
        held = _held_birth(state.births, key, new)
        sightings = (held[0] if held is not None else 0) + 1
        if sightings >= _BIRTH_CONFIRM:
            # Confirmed on a consecutive pass: real content. Of the readings
            # seen, the cleaner one is adopted — over video the first or
            # second read is often garbled ("Where wereyou]astunight?").
            if held is not None and reading_score(held[1].text) > reading_score(new.text):
                merged.append(replace(new, text=held[1].text))
            else:
                merged.append(new)
        else:
            kept.births[key] = (sightings, new)  # held one pass; junk never shows

    for prev_idx, old in enumerate(previous):
        if prev_idx in used_previous:
            continue  # paired again: its miss entry resets by omission
        if blank_check is not None and blank_check(old):
            continue  # area went uniform: the text is truly gone
        key = (old.left // _VOTE_BUCKET_PX, old.top // _VOTE_BUCKET_PX)
        count, since = state.misses.get(key, (0, now))
        count += 1
        if count >= _GHOST_MISS_LIMIT and now - since >= _GHOST_MIN_AGE_S:
            continue  # missed repeatedly AND long enough: the ghost dies
        kept.misses[key] = (count, since)
        merged.append(old)
    return merged, kept


_FRAGMENT_CONTAINMENT = 0.7  # of the fresh line's area inside the tracked line


def _fragment_of_tracked_line(previous: list[OcrLine], paired: set[int], new: OcrLine) -> bool:
    """True when ``new`` is a piece of a previous line that paired this pass.

    Over video, OCR splits one subtitle line into pieces differently each
    pass; the best piece pairs with the whole line and holds its text, but
    the OTHER pieces would be born as new lines and translated on their own
    ("where you are" -> "İşte buradasın" next to the full sentence).
    """
    new_area = max(1, new.width * new.height)
    new_norm = _norm_text(new.text)
    for index in paired:
        old = previous[index]
        overlap_w = min(old.left + old.width, new.left + new.width) - max(old.left, new.left)
        overlap_h = min(old.top + old.height, new.top + new.height) - max(old.top, new.top)
        if overlap_w <= 0 or overlap_h <= 0:
            continue
        if overlap_w * overlap_h / new_area >= _FRAGMENT_CONTAINMENT and (
            new_norm in _norm_text(old.text)
        ):
            return True
    return False


def _held_birth(
    births: dict[tuple[int, int], tuple[int, OcrLine]], key: tuple[int, int], new: OcrLine
) -> tuple[int, OcrLine] | None:
    """The held birth entry this fresh line re-sights, if any.

    The exact position bucket is tried first; otherwise any held line the
    fresh one overlaps like a stabilization pair with similar text counts.
    """
    entry = births.get(key)
    if entry is not None:
        return entry
    for count, held in births.values():
        if (
            _axis_overlap_fraction(held.top, held.height, new.top, new.height)
            >= _STABLE_PAIR_V_OVERLAP
            and _axis_overlap_fraction(held.left, held.width, new.left, new.width)
            >= _STABLE_PAIR_H_OVERLAP
            and SequenceMatcher(None, held.text.casefold(), new.text.casefold()).ratio()
            >= _BIRTH_MATCH_RATIO
        ):
            return count, held
    return None


_READING_STRIP = ".,!?…:;\"'()«»“”‘’"


def reading_score(text: str) -> float:
    """Share of tokens that look like clean words (0..1).

    A token is clean when, stripped of surrounding punctuation, it is made of
    letters (apostrophes/hyphens allowed) with a sane case pattern: all caps,
    or only the first letter upper. Brackets, digits inside words and
    mid-word capitals ("wereyou]astunight", "wOork") mark a garbled read.
    """
    tokens = text.split()
    if not tokens:
        return 0.0
    clean = 0
    for token in tokens:
        core = token.strip(_READING_STRIP)
        if not core:
            continue
        letters = core.replace("'", "").replace("-", "")
        if letters and letters.isalpha() and (letters.isupper() or letters[1:].islower()):
            clean += 1
    return clean / len(tokens)


def _stabilize_pair(
    old: OcrLine,
    new: OcrLine,
    ratio: float,
    votes: Votes,
    kept_votes: Votes,
    now: float = 0.0,
) -> OcrLine:
    """Choose the surviving text (and, for stable text, geometry) of a pair.

    >=0.93 similarity (or equality modulo whitespace/case) is OCR jitter: the
    old text wins, and the old GEOMETRY also wins unless the fresh box moved
    or resized materially — busy backgrounds jitter word boxes by 20-40%,
    which otherwise repaints stable text at a new size every pass. <0.55 is
    genuinely different text: the fresh one wins. In between, a fresh reading
    with MORE letters wins immediately ("Inspect" beats "nspect" whichever
    arrives first); otherwise the old text holds until the same normalized
    candidate has repeated _STABLE_VOTES_NEEDED times in a row.
    """
    key = (old.left // _VOTE_BUCKET_PX, old.top // _VOTE_BUCKET_PX)
    if ratio >= _STABLE_JITTER_RATIO or _norm_text(old.text) == _norm_text(new.text):
        if abs(new.angle_deg - old.angle_deg) > _ANGLE_VOTE_DEG:
            # A sudden tilt of unchanged text is the engine mis-estimating
            # skew on a busy frame (video): the mapped-back boxes tilt with
            # it, so hold the settled geometry until the tilt persists.
            tilt = f"\x00angle:{round(new.angle_deg)}"
            if _vote(votes, kept_votes, key, tilt, now):
                return replace(new, text=old.text)
            return old
        if _geometry_changed_materially(old, new):
            return replace(new, text=old.text)
        return old
    if _norm_text(new.text) in _norm_text(old.text):
        # A partial read of the same line ("Stay right where you are." for
        # "Don't move. Stay right where you are."): a busy background breaks
        # lines into pieces from pass to pass. The whole line holds unless
        # the shorter text keeps coming back (then it really changed).
        if _vote(votes, kept_votes, key, _norm_text(new.text), now):
            return new
        return old
    if ratio < _STABLE_CHANGE_RATIO:
        if reading_score(new.text) + _SCORE_DROP_MAX < reading_score(old.text):
            # A clean line "replaced" by a garbled one is noise, not a change
            # ("We have to leave" -> "Weha e e to wave e o e"): hold unless
            # the garbled reading itself proves persistent.
            if _vote(votes, kept_votes, key, _norm_text(new.text), now):
                return new
            return old
        return new
    if _norm_text(old.text) in _norm_text(new.text):
        return new  # the longer read finally saw every glyph ("nspect" -> "Inspect")
    if _vote(votes, kept_votes, key, _norm_text(new.text), now):
        return new  # consistent new reading: the change is real
    return replace(new, text=old.text)


def _vote(
    votes: Votes, kept_votes: Votes, key: tuple[int, int], candidate: str, now: float
) -> bool:
    """Count consecutive identical candidates; True once the change is trusted.

    Trust needs repetition AND time: _STABLE_VOTES_NEEDED identical readings
    spanning at least _STABLE_VOTE_MIN_S. A differing candidate restarts
    both. Untrusted candidates are carried in ``kept_votes``.
    """
    previous_candidate, count, first = votes.get(key, ("", 0, now))
    if candidate == previous_candidate:
        count += 1
    else:
        count, first = 1, now
    if count >= _STABLE_VOTES_NEEDED and now - first >= _STABLE_VOTE_MIN_S:
        return True
    kept_votes[key] = (candidate, count, first)
    return False


def _geometry_changed_materially(old: OcrLine, new: OcrLine) -> bool:
    """True when the fresh box is a real move/resize, not measurement jitter."""
    if abs(new.left - old.left) > 6 or abs(new.top - old.top) > 6:
        return True
    old_h = max(1, old.height)
    if not 0.87 <= new.height / old_h <= 1.15:
        return True
    old_w = max(1, old.width)
    if not 0.8 <= new.width / old_w <= 1.25:
        return True
    old_ink = old.ink_height or old.height
    new_ink = new.ink_height or new.height
    if old_ink and not 0.87 <= new_ink / old_ink <= 1.15:
        return True
    return abs(new.angle_deg - old.angle_deg) > 6.0


def _norm_text(text: str) -> str:
    return " ".join(text.split()).casefold()


def _axis_overlap_fraction(a_start: int, a_length: int, b_start: int, b_length: int) -> float:
    """Overlap of two 1-D spans as a fraction of the shorter span's length."""
    shorter = min(a_length, b_length)
    if shorter <= 0:
        return 0.0
    overlap = min(a_start + a_length, b_start + b_length) - max(a_start, b_start)
    return max(0.0, overlap / shorter)


def _dist(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def ink_fraction(frame: Image.Image, line: OcrLine, fg: str) -> float:
    """Share of the line's box painted (near) the text color ``fg``.

    Cheap presence test for the ghost logic: white subtitle glyphs over a
    video keep their share while the subtitle is up and lose it the frame it
    goes away, whereas a tree scrolling behind a menu label leaves the white
    glyph pixels alone. Pure PIL ops (difference + histogram), no Python loop.
    """
    left = max(0, line.left)
    top = max(0, line.top)
    right = min(frame.width, line.left + line.width)
    bottom = min(frame.height, line.top + line.height)
    if right <= left or bottom <= top:
        return 0.0
    crop = frame.crop((left, top, right, bottom)).convert("RGB")
    target = Image.new("RGB", crop.size, _parse_hex(fg))
    distance = ImageChops.difference(crop, target).convert("L")
    close = sum(distance.histogram()[:_INK_TOLERANCE])
    return close / (crop.width * crop.height)


def _parse_hex(color: str) -> tuple[int, int, int]:
    try:
        return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    except (ValueError, IndexError):
        return 255, 255, 255


def _luminance(rgb: tuple[int, ...]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _hex(rgb: tuple[int, ...]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
