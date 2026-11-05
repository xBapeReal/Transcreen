"""Application core: a continuous whole-screen live translation loop.

No windows, no buttons: the app starts, watches the screen, and paints the
Turkish translation directly over every piece of foreign text, matching its
position and size. Ctrl+Alt+T pauses, Ctrl+Alt+Q quits.

Speed architecture:
- mss-based grabs (~10-15 ms) feed a tiny grayscale signature; nothing else
  runs while the screen is unchanged.
- While the screen is in motion (scroll, video) OCR waits for stability up to
  a short deadline, so blurred frames are not read.
- Only the changed region (dirty tiles) is OCR'd; lines outside it are kept.
- Translations are fetched on a separate thread; frames render cached lines
  instantly and repaint when the missing ones arrive.
- When the offline en->tr model is installed, new text is provisionally
  painted from it within about one cycle and silently upgraded to the cloud
  translation as soon as one arrives.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import queue
import re
import threading
import time
import tkinter as tk
from collections.abc import Callable
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageGrab, ImageStat

from . import winfx
from .config import Config, config_dir
from .hotkeys import MOD_ALT, MOD_CONTROL, VK_Q, VK_S, VK_T, HotkeyManager
from .local_mt import LocalTranslator, default_model_dir
from .ocr import OcrLine, OcrUnavailableError, ParallelOcr, merge_lines
from .overlay import OverlayItem, ScreenOverlay
from .screen import Rect, grab_virtual_screen_fast, virtual_screen
from .translator import BackgroundTranslator, Translator
from .vision import (
    StabilizeState,
    dedupe_overlapping_lines,
    dirty_rect,
    fix_ocr_digits,
    font_px_for_line,
    frame_signature,
    group_blocks,
    ink_fraction,
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

logger = logging.getLogger(__name__)

POLL_MS = 6  # UI queue pump interval
_MOTION_MAX_WAIT_S = 0.12  # OCR anyway after this much continuous motion
_GHOST_RECHECK_S = 0.6  # re-OCR ghost areas this soon even on a static screen
_MISS_BUCKET_PX = 8  # must match vision's stabilize_lines miss-key bucketing
_SCENE_RESET_FRACTION = 0.55  # dirty coverage above this = page switch: reset damping
_FULL_FRAME_FRACTION = 0.45  # dirty coverage above this -> full-frame OCR
_ERROR_TOAST_EVERY_S = 10.0
_PAUSE_SLEEP_S = 0.15
_OCR_WORKERS = max(2, min(4, (os.cpu_count() or 4) - 1))
_VK_LBUTTON = 0x01
_INTERACTION_GRACE_S = 1.5  # selection-reconcile window after releasing the mouse
_SS_MODE_MS = 8000  # screenshot mode duration
_PROTECTED_POLL_S = 0.4  # how often to re-check a DRM window's focus
_BLANK_STDDEV = 6.0  # grayscale stddev below this = the line's area went blank
_OVERSIZE_HEIGHT_FACTOR = 2.2  # junk guard: box taller than this x median height
_OVERSIZE_MAX_LETTERS = 4  # ...while carrying at most this many letters
_FONT_CAP_FACTOR = 1.15  # single-line em size never exceeds height * this
# Ink-height hysteresis: a busy background swings the same word's OCR box by
# tens of percent (measured 30 -> 56 px on one menu button), which repainted
# the translation at a new size every pass. Reuse the remembered height while
# the fresh one stays inside this band.
_INK_STABLE_BAND = (0.75, 1.33)
_STICKY_BUCKET_PX = 32  # jitter-tolerant key for remembered ink/paint
_STICKY_PAINT_S = 2.5  # keep showing the last translation while a re-read is pending
_STICKY_PAINT_SIMILARITY = 0.8  # ...only while the source text still matches
# Cover boxes only grow within this window. Stylized logos OCR as different
# fragments each pass ("STARFALL" / "Rise of STARFALL" / "FALL"), which made
# the cover jump between 439 and 809 px wide; unioning recent boxes pins it.
_STICKY_BOX_S = 2.5
_COLOR_CHANGE_MIN = 70  # channel-sum distance that counts as a real color change
_COLOR_EASE = 0.4  # ...and even then the cover only moves this far per pass
_INLINE_LOCAL_MAX = 4  # misses per pass translated right in the loop by the local model
# Reading stability: once a translation has been on screen this long, a late
# cloud "upgrade" or a re-read variant no longer rewrites it under the
# reader's eyes; the newer text serves the next sighting of that sentence.
_HOLD_AFTER_S = 1.0
_SAME_SENTENCE_RATIO = 0.8  # source similarity that still counts as the same sentence
_LONGER_READ_LETTERS = 3  # a re-read with this many more letters is a correction
# Ghost logic: the text is gone when its ink share drops below this fraction
# of the share measured when it was painted (baselines fainter than
# _INK_MIN_BASELINE are too weak to judge and fall back to the timed TTL).
_INK_VANISH_RATIO = 0.35
_INK_MIN_BASELINE = 0.02
_LETTER_RE = re.compile(r"[^\W\d_]")  # any-script letter
_MIN_READING_SCORE = 0.3  # blocks read this badly are not painted at all

# Candidate geometries waiting for a confirming second sighting (see
# _sticky_box). Module-level: the live loop owns exactly one App.
_pending_boxes: dict[tuple[int, int], tuple[int, int, int, int]] = {}
# Last colors painted at a spot, so a scrolling background cannot make the
# cover pulse between shades (see _stable_color).
_last_colors: dict[tuple[int, int], tuple[str, str, bool]] = {}


class App:
    def __init__(self, cfg: Config, smoke_dir: str | None = None) -> None:
        self.cfg = cfg
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Transcreen")

        self._ui_q: queue.Queue[Callable[[], None]] = queue.Queue()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._force_redraw = threading.Event()
        self._ss_hold = threading.Event()
        self._ss_after: str | None = None
        self._interaction_until = 0.0
        self._worker: threading.Thread | None = None
        self._smoke_dir = smoke_dir
        self._last_error_toast = 0.0
        self.last_items: list[OverlayItem] = []  # last drawn items (diagnostics)
        self.last_lines: list[OcrLine] = []  # stabilized lines of the last OCR pass
        self.last_stab = StabilizeState()  # damping state of the last OCR pass

        self.ocr = ParallelOcr(cfg.ocr_lang, workers=_OCR_WORKERS)
        # Offline en->tr tier: constructed cheaply here (no model load), fed
        # into the translator as its provisional prefill backend.
        self.local_mt: LocalTranslator | None = None
        if cfg.use_local_mt and cfg.target_lang == "tr":
            self.local_mt = LocalTranslator(default_model_dir())
        self.translator = Translator(cfg.target_lang, local=self.local_mt)
        self.translator.enable_persistence(config_dir() / "cache.json")
        self.bg_translator = BackgroundTranslator(
            self.translator,
            on_ready=self._force_redraw.set,
            on_error=self._translation_error,
        )
        vs = self._vs = virtual_screen()
        # Smoke mode keeps the overlay capturable so we can screenshot it.
        self.overlay = ScreenOverlay(self.root, vs, capture_excluded=smoke_dir is None)

        self.hotkeys = HotkeyManager()
        self.hotkeys.add(
            MOD_CONTROL | MOD_ALT, VK_T, "Ctrl+Alt+T", lambda: self.post(self.toggle_pause)
        )
        self.hotkeys.add(MOD_CONTROL | MOD_ALT, VK_Q, "Ctrl+Alt+Q", lambda: self.post(self.quit))
        self.hotkeys.add(
            MOD_CONTROL | MOD_ALT,
            VK_S,
            "Ctrl+Alt+S",
            lambda: self.post(self.toggle_screenshot_mode),
        )
        self.hotkeys.start()

        self.root.after(POLL_MS, self._drain)
        self.root.after(900, self._report_hotkey_failures)
        if smoke_dir is not None:
            self._schedule_smoke(Path(smoke_dir))
        else:
            self.bg_translator.start()
            self._start_local_mt()
            # Give the overlay time to apply click-through + capture exclusion
            # before the first grab, or we would OCR our own pixels.
            self.root.after(350, self._start_worker)
            self.root.after(
                600,
                lambda: self.overlay.show_toast(
                    "Transcreen canlı  •  Ctrl+Alt+T duraklat  •  "
                    "Ctrl+Alt+S ekran görüntüsü  •  Ctrl+Alt+Q çıkış",
                    5000,
                ),
            )

    # ------------------------------------------------------------- plumbing
    def post(self, fn: Callable[[], None]) -> None:
        """Thread-safe: queue a callable to run on the UI thread."""
        self._ui_q.put(fn)

    def _drain(self) -> None:
        while True:
            try:
                fn = self._ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                logger.exception("UI task failed")
        self.root.after(POLL_MS, self._drain)

    def _report_hotkey_failures(self) -> None:
        if self.hotkeys.failed:
            joined = ", ".join(self.hotkeys.failed)
            self.overlay.show_toast(f"⚠ Kısayol alınamadı: {joined}", 4000)

    def _translation_error(self, _message: str) -> None:
        now = time.monotonic()
        if now - self._last_error_toast > _ERROR_TOAST_EVERY_S:
            self._last_error_toast = now
            self.post(
                lambda: self.overlay.show_toast(
                    "⚠ Çeviri alınamadı — internet bağlantısını kontrol et", 2500
                )
            )

    def _start_local_mt(self) -> None:
        """Kick off the local model's cold load, or hint at --setup-local once.

        The hint waits for the welcome toast to finish; a missing model is a
        normal cloud-only configuration, so it is a suggestion, not an error.
        """
        if self.local_mt is None:
            return
        if _local_model_installed(default_model_dir()):
            self.local_mt.start_loading()
            return
        self.root.after(
            5800,
            lambda: self.overlay.show_toast(
                "Lokal çeviri modeli kurulu değil — anında çeviri için: "
                "python -m transcreen --setup-local",
                6000,
            ),
        )

    # ------------------------------------------------------------ live loop
    def _start_worker(self) -> None:
        self._worker = threading.Thread(target=self._loop, name="live-loop", daemon=True)
        self._worker.start()

    def _loop(self) -> None:
        lines: list[OcrLine] = []
        # Cross-frame stabilization state (vision.stabilize_lines): votes damp
        # mid-similarity re-readings, misses give unpaired lines a timed ghost
        # TTL, births hold brand-new lines for a confirming second sighting.
        stab = StabilizeState()
        ghost_recheck_at: float | None = None
        blocks: list[list[OcrLine]] = []
        block_texts: list[str] = []
        # Per-block (prefix, core, suffix) from split_quantity_affixes: only
        # the core is translated/cached ("x2 BANDAGES" -> "BANDAGES"), the
        # affixes are re-attached verbatim at draw time.
        block_affixes: list[tuple[str, str, str]] = []
        # Per-block (bg, fg, bold) cache so force-redraws (translations
        # arriving) never rerun pick_colors for unchanged pixels.
        colors: dict[tuple[int, int, str], tuple[str, str, bool]] = {}
        # Per-spot memories keyed loosely enough to survive box jitter:
        # remembered ink heights kill font-size flapping, remembered paints
        # keep a translation on screen while its slightly-rewritten source is
        # being re-translated.
        inks: dict[tuple[int, int], int] = {}
        paints: dict[tuple[int, int], _Paint] = {}
        boxes: dict[tuple[int, int], tuple[int, int, int, int, float, str]] = {}
        # Per-line (text color, ink share) measured when a block was painted:
        # lets the ghost logic tell "OCR missed it" from "it is gone".
        ink_marks: dict[tuple[int, int], tuple[str, float]] = {}
        prev_sig: bytes | None = None
        ocr_sig: bytes | None = None
        motion_start: float | None = None
        sustained = False  # the previous OCR pass already saw a page-sized change
        last_key: tuple | None = None
        protected = False  # DRM window in focus: overlay stands down entirely

        def render(frame: Image.Image) -> int:
            """Paint the current blocks from the cache; returns the miss count.

            Subtitle-sized miss batches are translated right here by the
            local model (one pass, no thread hop, no OCR in between); bigger
            ones go to the background thread. Whatever still misses is handed
            to the cloud tier.
            """
            nonlocal last_key
            cores = [core for _prefix, core, _suffix in block_affixes]
            cached = self.translator.try_cached(cores)
            missing = [t for t, c in zip(cores, cached, strict=True) if c is None]
            if 0 < len(missing) <= _INLINE_LOCAL_MAX and self.translator.prefill_local(
                missing, autosave=False
            ):
                cached = self.translator.try_cached(cores)
                missing = [t for t, c in zip(cores, cached, strict=True) if c is None]
            if missing:
                self.bg_translator.request(missing)
            # On-screen text still served by the provisional local tier is
            # queued for a silent cloud quality upgrade.
            upgrades = self.translator.pending_upgrade(cores)
            if upgrades:
                self.bg_translator.request_upgrade(upgrades)
            items = self._build_items(
                blocks,
                block_texts,
                block_affixes,
                cached,
                frame,
                colors,
                inks,
                paints,
                boxes,
                ink_marks,
            )
            key = tuple((it.left // 3, it.top // 3, it.text, it.bg, it.fg, it.bold) for it in items)
            if key != last_key:
                last_key = key
                self.last_items = list(items)  # exposed for tests/diagnostics
                self.post(partial(self.overlay.set_items, list(items)))
            return len(missing)

        while not self._stop.is_set():
            if self._paused.is_set() or self._ss_hold.is_set():
                time.sleep(_PAUSE_SLEEP_S)
                continue
            if winfx.foreground_is_protected():
                # DRM playback (Netflix and friends) refuses to render while a
                # capture-related overlay sits in front of it: stand down
                # completely until that window loses focus.
                if not protected:
                    protected = True
                    logger.info("protected content in focus: pausing the overlay")
                    lines, stab = [], StabilizeState()
                    self.post(self._stand_down)
                time.sleep(_PROTECTED_POLL_S)
                continue
            if protected:
                protected = False
                ocr_sig = prev_sig = None  # rescan the screen from scratch
                self._force_redraw.set()
            if ctypes.windll.user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000:
                # The user is dragging/selecting: freeze updates. The release
                # opens a grace window in which selection-garbled OCR gets
                # reconciled against the previous stable lines.
                self._interaction_until = time.monotonic() + _INTERACTION_GRACE_S
                time.sleep(0.05)
                continue
            try:
                t0 = time.perf_counter()
                frame = grab_virtual_screen_fast()
                sig = frame_signature(frame)
                moving = prev_sig is not None and signatures_differ(sig, prev_sig)
                prev_sig = sig
                now = time.monotonic()

                if self._force_redraw.is_set():
                    # Translations arrived (or a resume): paint from the
                    # current blocks right now, never behind an OCR pass.
                    self._force_redraw.clear()
                    render(frame)

                if moving:
                    if motion_start is None:
                        motion_start = now
                    if now - motion_start < _MOTION_MAX_WAIT_S:
                        # Motion just began (scroll, transition): let it
                        # settle briefly so blurred frames are not read.
                        time.sleep(self.cfg.idle_poll)
                        continue
                    # Sustained motion (video): every pass reads the live
                    # frame. The settle window is NOT re-armed — doing so
                    # made every subtitle pay it again (~150 ms per pass).
                else:
                    motion_start = None

                region, fraction = dirty_rect(ocr_sig, sig, frame.width, frame.height)
                if (
                    region is None
                    and (stab.misses or stab.births or stab.votes)
                    and ghost_recheck_at is not None
                    and time.monotonic() >= ghost_recheck_at
                ):
                    # Static screen with pending ghosts OR unconfirmed births:
                    # counters only advance during OCR passes, so without this
                    # forced pass a once-missed line would survive forever and
                    # a once-seen NEW line would never get its confirming
                    # second sighting (it would never translate until the
                    # user alt-tabbed and forced a scene reset).
                    region = _pending_region(lines, stab, frame)
                    fraction = 0.0
                    if region is None:
                        ghost_recheck_at = None  # nothing locatable to recheck
                if region is None:
                    sustained = False  # a still frame: the next big change is a switch
                    time.sleep(self.cfg.idle_poll)
                    continue
                t1 = time.perf_counter()

                if region is not None:
                    ocr_sig = sig
                    # A page-sized change is a scene switch only when it comes
                    # out of stillness; back-to-back page-sized changes are a
                    # video, which needs the damping (jitter, births, ghosts).
                    page_switch = fraction >= _SCENE_RESET_FRACTION and not sustained
                    sustained = fraction >= _SCENE_RESET_FRACTION
                    lines, stab = self._refresh_lines(
                        lines, stab, frame, region, fraction, page_switch, ink_marks
                    )
                    ghost_recheck_at = (
                        time.monotonic() + _GHOST_RECHECK_S
                        if (stab.misses or stab.births or stab.votes)
                        else None
                    )
                    lines = _drop_oversized_junk(lines)
                    self.last_lines, self.last_stab = list(lines), stab
                    # Spaced-out words (game subtitles) come back as separate
                    # fragments: re-join rows first, then group rows into
                    # paragraph blocks so the translator sees full sentences.
                    raw_blocks = group_blocks(merge_row_fragments(dedupe_overlapping_lines(lines)))
                    raw_texts = [
                        fix_ocr_digits(merge_lines([line.text for line in block]))
                        for block in raw_blocks
                    ]
                    # Text already in Turkish is neither requested nor covered.
                    survivors = [
                        (block, text)
                        for block, text in zip(raw_blocks, raw_texts, strict=True)
                        if not looks_turkish(text)
                    ]
                    blocks = [block for block, _text in survivors]
                    block_texts = [text for _block, text in survivors]
                    block_affixes = [split_quantity_affixes(text) for text in block_texts]
                    # Keep color entries only for surviving blocks OUTSIDE the
                    # OCR'd region: dirty pixels under a block mean its
                    # background may have changed (animation), so it gets
                    # resampled from this fresh frame. Force-redraw passes
                    # (region is None) still hit the cache untouched.
                    keep_keys = {
                        _color_key(rep)
                        for rep in (
                            _block_rep(block, text)
                            for block, text in zip(blocks, block_texts, strict=True)
                        )
                        if not _intersects(rep, region)
                    }
                    colors = {k: v for k, v in colors.items() if k in keep_keys}
                    # Ink heights survive a re-OCR of their own area: the box
                    # breathing IS the jitter we are damping. They are pruned
                    # only when the block itself disappears.
                    live_keys = {
                        _sticky_key(_block_rep(block, text))
                        for block, text in zip(blocks, block_texts, strict=True)
                    }
                    inks = {k: v for k, v in inks.items() if k in live_keys}
                    cutoff = time.monotonic() - _STICKY_PAINT_S
                    paints = {
                        k: v for k, v in paints.items() if k in live_keys and v.seen >= cutoff
                    }
                    boxes = {k: v for k, v in boxes.items() if k in live_keys and v[4] >= cutoff}
                    line_keys = {_sticky_key(line) for block in blocks for line in block}
                    ink_marks = {k: v for k, v in ink_marks.items() if k in line_keys}
                t2 = time.perf_counter()
                pending = render(frame)
                logger.debug(
                    "cycle grab+sig=%.0fms ocr=%.0fms build=%.0fms lines=%d "
                    "pending=%d dirty=%.0f%%",
                    (t1 - t0) * 1000,
                    (t2 - t1) * 1000,
                    (time.perf_counter() - t2) * 1000,
                    len(lines),
                    pending,
                    fraction * 100,
                )
            except OcrUnavailableError as exc:
                logger.error("OCR unavailable: %s", exc)
                self.post(
                    lambda: self.overlay.show_toast(
                        "⚠ Windows OCR dil paketi yok — Ayarlar > Zaman ve Dil > Dil", 0
                    )
                )
                return
            except Exception:
                if self._stop.is_set():
                    break  # teardown race: executors are already shutting down
                logger.exception("live loop cycle failed")
                time.sleep(1.0)

    def _refresh_lines(
        self,
        lines: list[OcrLine],
        stab: StabilizeState,
        frame: Image.Image,
        region: Rect,
        fraction: float,
        page_switch: bool = True,
        ink_marks: dict[tuple[int, int], tuple[str, float]] | None = None,
    ) -> tuple[list[OcrLine], StabilizeState]:
        """OCR the dirty region only; keep prior lines outside it.

        Fresh readings never replace stable text directly: after the
        selection-reconcile window they run through vision.stabilize_lines
        against the previous lines of the same area (votes + birth debounce +
        timed ghost TTL, see vision.StabilizeState). A dirty coverage at or
        above _SCENE_RESET_FRACTION means a page switch: damping state is
        reset and the new screen is adopted instantly, so nothing from the
        old page can linger or delay the new one.
        """
        full_frame = fraction >= _FULL_FRAME_FRACTION or (
            region.width >= frame.width and region.height >= frame.height
        )
        scene_reset = page_switch and fraction >= _SCENE_RESET_FRACTION
        if full_frame:
            crop, offset_x, offset_y = frame, 0, 0
        else:
            crop = frame.crop(region.as_bbox())
            offset_x, offset_y = region.left, region.top

        recognized = [
            OcrLine(
                text=line.text,
                left=line.left + offset_x,
                top=line.top + offset_y,
                width=line.width,
                height=line.height,
                angle_deg=line.angle_deg,
                ink_height=line.ink_height,
                run_length=line.run_length,
                anchor_x=line.anchor_x + offset_x if line.anchor_x >= 0 else -1,
                anchor_y=line.anchor_y + offset_y if line.anchor_y >= 0 else -1,
            )
            for line in self.ocr.recognize_lines(crop)
        ]
        fresh = [
            line
            for line in recognized
            if line.height >= self.cfg.min_text_height and is_real_text(line.text)
        ]
        now = time.monotonic()
        interacting = now < self._interaction_until
        blank_check = partial(_line_vanished, frame, ink_marks or {})
        if full_frame:
            if interacting and not scene_reset:
                fresh = reconcile_selection(lines, fresh)
            return stabilize_lines(
                lines, fresh, stab, now=now, blank_check=blank_check, scene_reset=scene_reset
            )
        kept = [line for line in lines if not _intersects(line, region)]
        evicted = [line for line in lines if _intersects(line, region)]
        if interacting:
            fresh = reconcile_selection(evicted, fresh)
        stable, new_stab = stabilize_lines(evicted, fresh, stab, now=now, blank_check=blank_check)
        # A partial pass can only judge entries inside its region; carry the
        # rest of the damping state over untouched so ghosts/births elsewhere
        # keep their timers and counters.
        _preserve_outside_state(stab, new_stab, region)
        return kept + stable, new_stab

    def _build_items(
        self,
        blocks: list[list[OcrLine]],
        block_texts: list[str],
        block_affixes: list[tuple[str, str, str]],
        cached: list[str | None],
        frame: Image.Image,
        colors: dict[tuple[int, int, str], tuple[str, str, bool]],
        inks: dict[tuple[int, int], int] | None = None,
        paints: dict[tuple[int, int], _Paint] | None = None,
        boxes: dict[tuple[int, int], tuple[int, int, int, int, float, str]] | None = None,
        ink_marks: dict[tuple[int, int], tuple[str, float]] | None = None,
    ) -> list[OverlayItem]:
        items: list[OverlayItem] = []
        if inks is None:
            inks = {}
        if paints is None:
            paints = {}
        if boxes is None:
            boxes = {}
        if ink_marks is None:
            ink_marks = {}
        now = time.monotonic()
        for block, source_text, (prefix, core, suffix), translated in zip(
            blocks, block_texts, block_affixes, cached, strict=True
        ):
            rep = _block_rep(block, source_text)
            sticky = _sticky_key(rep)
            previous = paints.get(sticky)
            if reading_score(source_text) < _MIN_READING_SCORE:
                # Mostly garbled read ("Weha e e to wave e o e"): its
                # translation would be garbage too. Leave the original
                # pixels alone until a cleaner pass comes through.
                continue
            if not translated:
                # Pending re-read of text we already painted: keep the last
                # translation on screen instead of blinking out.
                translated = _sticky_paint(paints, sticky, source_text, now)
                if not translated:
                    continue
                display = translated
                paints[sticky] = _Paint(source_text, display, now, now, provisional=True)
            else:
                if _norm(translated) == _norm(core):
                    continue  # already Turkish: leave the original pixels alone
                # Quantity affixes survive verbatim: "x2 " + "BANDAJ" + "".
                display = f"{prefix}{translated}{suffix}"
                first = now
                if (
                    previous is not None
                    and not previous.provisional
                    and _same_sentence(previous.source, source_text)
                ):
                    first = previous.first
                    if (
                        display != previous.display
                        and now - first > _HOLD_AFTER_S
                        and not _longer_read(previous.source, source_text)
                    ):
                        # A reader is mid-sentence: a late "upgrade" or a
                        # re-read variant must not rewrite the line under
                        # their eyes. The newer text serves the next sighting.
                        display = previous.display
                paints[sticky] = _Paint(source_text, display, now, first, provisional=False)
            key = _color_key(rep)
            triple = colors.get(key)
            if triple is None:
                triple = colors[key] = _stable_color(
                    _last_colors.get(sticky), pick_colors(frame, rep)
                )
                # Fresh pixels under this block: remember each line's ink
                # share so the ghost logic can tell "OCR missed it" from
                # "it is gone" (see _line_vanished). A pass whose share
                # collapsed is exactly the "gone" case: it must not lower the
                # baseline (or replace the text color) before the check runs.
                for line in block:
                    line_key = _sticky_key(line)
                    share = ink_fraction(frame, line, triple[1])
                    previous_mark = ink_marks.get(line_key)
                    if previous_mark is None or share >= previous_mark[1] * 0.6:
                        ink_marks[line_key] = (triple[1], share)
            _last_colors[sticky] = bg, fg, bold = triple
            if len(block) == 1:
                line = block[0]
                # Normalize the OcrLine sentinels so the overlay always gets
                # concrete rotation geometry (upright lines carry them unused).
                ink = _stable_ink(inks, sticky, line.ink_height or line.height)
                box_left, box_top, box_width, box_height = (
                    _sticky_box(boxes, sticky, line, now, source_text)
                    if line.angle_deg == 0.0
                    else (line.left, line.top, line.width, line.height)
                )
                items.append(
                    OverlayItem(
                        left=box_left,
                        top=box_top,
                        width=box_width,
                        height=box_height,
                        text=display,
                        bg=bg,
                        fg=fg,
                        # Cap the em size near the box height: stylized fonts
                        # would otherwise yield covers far taller than the box.
                        font_px=min(
                            font_px_for_line(line.text, ink),
                            round(box_height * _FONT_CAP_FACTOR),
                        ),
                        angle_deg=line.angle_deg,
                        ink_height=ink,
                        run_length=(line.run_length or line.width) if line.angle_deg else box_width,
                        anchor_x=(
                            line.anchor_x if line.angle_deg and line.anchor_x >= 0 else box_left
                        ),
                        anchor_y=(
                            line.anchor_y
                            if line.angle_deg and line.anchor_y >= 0
                            else box_top + box_height // 2
                        ),
                        bold=bold,
                    )
                )
            else:
                heights = sorted((line.ink_height or line.height) for line in block)
                ink = _stable_ink(inks, sticky, heights[len(heights) // 2])
                box_left, box_top, box_width, box_height = _sticky_box(
                    boxes, sticky, rep, now, source_text
                )
                items.append(
                    OverlayItem(
                        left=box_left,
                        top=box_top,
                        width=box_width,
                        height=box_height,
                        text=display,
                        bg=bg,
                        fg=fg,
                        font_px=font_px_for_line(source_text, ink),
                        ink_height=ink,
                        bold=bold,
                        wrap_width=box_width,
                        source_lines=tuple(
                            (line.left, line.top, line.width, line.height) for line in block
                        ),
                    )
                )
        return _drop_duplicate_items(items)

    # ------------------------------------------------------------- controls
    def toggle_pause(self) -> None:
        if self._paused.is_set():
            self._paused.clear()
            self._force_redraw.set()
            with contextlib.suppress(tk.TclError):
                self.overlay.deiconify()
            self.overlay.show_toast("Çeviri devam ediyor", 1500)
        else:
            self._paused.set()
            self.overlay.clear()
            self.overlay.show_toast("Duraklatıldı  •  Ctrl+Alt+T devam ettirir", 1500)

            def hide_if_still_paused() -> None:
                if self._paused.is_set():
                    with contextlib.suppress(tk.TclError):
                        self.overlay.withdraw()

            self.root.after(1600, hide_if_still_paused)

    def _stand_down(self) -> None:
        """Drop every drawn item and unmap the overlay window (UI thread)."""
        self.last_items = []
        with contextlib.suppress(tk.TclError):
            self.overlay.clear()
            self.overlay.sync_visibility()

    def toggle_screenshot_mode(self) -> None:
        """Temporarily include the overlay in screen captures (Ctrl+Alt+S).

        The overlay is normally invisible to every capture path, so the user's
        own screenshots miss the translations; this pauses the loop and lifts
        the exclusion for a few seconds so Win+Shift+S catches everything.
        """
        if self._ss_hold.is_set():
            self._end_ss_mode()
            return
        self._ss_hold.set()
        winfx.set_capture_exclusion(self.overlay, excluded=False)
        self.overlay.show_toast(
            "📸 Ekran görüntüsü modu: 8 sn — çeviriler görüntüye dahil", _SS_MODE_MS
        )
        self._ss_after = self.root.after(_SS_MODE_MS, self._end_ss_mode)

    def _end_ss_mode(self) -> None:
        if self._ss_after is not None:
            with contextlib.suppress(ValueError, tk.TclError):
                self.root.after_cancel(self._ss_after)
            self._ss_after = None
        if self._ss_hold.is_set():
            self._ss_hold.clear()
            if self._smoke_dir is None:
                winfx.set_capture_exclusion(self.overlay, excluded=True)
            self._force_redraw.set()

    def quit(self) -> None:
        self._stop.set()
        self.bg_translator.stop()
        self.hotkeys.stop()
        self.ocr.shutdown()
        self.translator.save_cache()
        self.cfg.save()
        with contextlib.suppress(tk.TclError):
            self.root.destroy()

    # ---------------------------------------------------------------- smoke
    def _schedule_smoke(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        sample_specs = [
            ("Ayarlar ve gizlilik seçenekleri", 240, 220, 420, 34, "#1f2430", "#e6e9f2"),
            (
                "Hızlı kahverengi tilki tembel köpeğin üzerinden atlar.",
                240,
                275,
                520,
                20,
                "#ffffff",
                "#1a1a1a",
            ),
            ("Dosya  Düzenle  Görünüm", 240, 310, 180, 15, "#f3f3f3", "#333333"),
        ]
        samples = [
            OverlayItem(
                left=left,
                top=top,
                width=width,
                height=height,
                text=text,
                bg=bg,
                fg=fg,
                font_px=font_px_for_line(text, height),
            )
            for text, left, top, width, height, bg, fg in sample_specs
        ]
        # One rotated banner (angle -20 = baseline ascends left-to-right) so
        # smoke screenshots prove the rotated render path visually.
        banner = "Eğik yazılar da artık destekleniyor"
        samples.append(
            OverlayItem(
                left=256,
                top=371,
                width=384,
                height=161,
                text=banner,
                bg="#31406b",
                fg="#ffd166",
                font_px=font_px_for_line(banner, 26),
                angle_deg=-20.0,
                ink_height=26,
                run_length=400,
                anchor_x=260,
                anchor_y=520,
            )
        )

        def show() -> None:
            self.overlay.set_items(samples)
            self.overlay.show_toast(
                "Transcreen canlı  •  Ctrl+Alt+T duraklat  •  Ctrl+Alt+Q çıkış", 0
            )

        def shot() -> None:
            with contextlib.suppress(tk.TclError, OSError):
                self.overlay.update_idletasks()
                vs = self._vs
                bbox = (vs.left + 180, vs.top + 180, vs.left + 860, vs.top + vs.height - 20)
                path = out_dir / "smoke_overlay.png"
                ImageGrab.grab(bbox=bbox, include_layered_windows=True, all_screens=True).save(path)
                print(f"SMOKE saved {path}", flush=True)

        self.root.after(700, show)
        self.root.after(1600, shot)
        self.root.after(2100, self.quit)

    def run(self) -> None:
        self.root.mainloop()


def _local_model_installed(model_dir: Path) -> bool:
    """Cheap existence check for the local model payload (no load, no import)."""
    return model_dir.is_dir() and any(model_dir.rglob("model.bin"))


def _drop_duplicate_items(items: list[OverlayItem]) -> list[OverlayItem]:
    """Never draw the same translation twice over one another.

    Two slightly different OCR readings of one button ("PLAY" / "PIAY") both
    translate to "OYNA" and survive line-level dedup, which paints the label
    twice at overlapping positions — the doubled subtitle seen in game. The
    larger box wins.
    """
    kept: list[OverlayItem] = []
    for item in sorted(items, key=lambda it: -(it.width * it.height)):
        duplicate = False
        for other in kept:
            if _norm(other.text) != _norm(item.text):
                continue
            overlap_x = min(other.left + other.width, item.left + item.width) - max(
                other.left, item.left
            )
            overlap_y = min(other.top + other.height, item.top + item.height) - max(
                other.top, item.top
            )
            if overlap_x > 0 and overlap_y > 0:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
    return sorted(kept, key=lambda it: (it.top, it.left))


class _Paint(NamedTuple):
    """What was last painted at a spot (see _build_items' hold rule)."""

    source: str  # OCR text the display was made for
    display: str  # text on screen
    seen: float  # last pass that painted it
    first: float  # when this sentence first got a real (non-provisional) paint
    provisional: bool  # reused from an older paint while a re-read was pending


def _same_sentence(old: str, new: str) -> bool:
    a, b = _norm(old), _norm(new)
    return a == b or SequenceMatcher(None, a, b).ratio() >= _SAME_SENTENCE_RATIO


def _longer_read(old: str, new: str) -> bool:
    """The re-read saw clearly more glyphs: a correction, not jitter."""
    return len(_LETTER_RE.findall(new)) - len(_LETTER_RE.findall(old)) >= _LONGER_READ_LETTERS


def _sticky_key(line: OcrLine) -> tuple[int, int]:
    """Position-only key that survives the few-px jitter of a busy background."""
    return (line.left // _STICKY_BUCKET_PX, line.top // _STICKY_BUCKET_PX)


def _stable_ink(inks: dict[tuple[int, int], int], key: tuple[int, int], fresh: int) -> int:
    """Remembered ink height for this spot, unless it really changed size."""
    previous = inks.get(key)
    low, high = _INK_STABLE_BAND
    if previous is not None and previous > 0 and low <= fresh / previous <= high:
        return previous
    inks[key] = fresh
    return fresh


def _sticky_box(
    boxes: dict[tuple[int, int], tuple[int, int, int, int, float, str]],
    key: tuple[int, int],
    line: OcrLine,
    now: float,
    text: str = "",
) -> tuple[int, int, int, int]:
    """Hold the box a spot settled on; adopt a new one only when it repeats.

    Both failure modes were seen live on the game menu: a shrinking box
    exposes the original text again (half-covered logo), and a single
    overshooting read inflates the cover (a menu button grew 46 -> 62 px when
    a tree darkened its background). So the remembered box wins while the
    fresh one is merely jittering, and a genuinely different box must show up
    twice in a row before it replaces it. Different TEXT at the spot (the
    next subtitle) is a new element: its box is adopted at once, or the old
    line's cover would outrun the new, shorter line for a pass.
    """
    fresh = (line.left, line.top, line.left + line.width, line.top + line.height)
    norm = _norm(text)
    remembered = boxes.get(key)
    if remembered is None or now - remembered[4] > _STICKY_BOX_S or remembered[5] != norm:
        boxes[key] = (*fresh, now, norm)
        _pending_boxes.pop(key, None)
        return line.left, line.top, line.width, line.height

    old = remembered[:4]
    if _boxes_close(old, fresh):
        boxes[key] = (*old, now, norm)  # jitter: keep the settled geometry
        return old[0], old[1], old[2] - old[0], old[3] - old[1]

    pending = _pending_boxes.get(key)
    if pending is not None and _boxes_close(pending, fresh):
        boxes[key] = (*fresh, now, norm)  # the new geometry repeated: adopt it
        _pending_boxes.pop(key, None)
        return line.left, line.top, line.width, line.height

    _pending_boxes[key] = fresh
    boxes[key] = (*old, now, norm)
    return old[0], old[1], old[2] - old[0], old[3] - old[1]


def _boxes_close(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """True when two boxes differ only by OCR jitter, not by a real change."""
    if abs(a[0] - b[0]) > 8 or abs(a[1] - b[1]) > 8:
        return False
    width_a, width_b = max(1, a[2] - a[0]), max(1, b[2] - b[0])
    height_a, height_b = max(1, a[3] - a[1]), max(1, b[3] - b[1])
    return 0.8 <= width_b / width_a <= 1.25 and 0.8 <= height_b / height_a <= 1.25


def _stable_color(
    old: tuple[str, str, bool] | None, fresh: tuple[str, str, bool]
) -> tuple[str, str, bool]:
    """Keep the remembered colors unless the pixels really changed hue.

    A tree scrolling behind a menu label swings the sampled background, which
    made the cover pulse between shades every pass; over video every pass
    samples a different shade. Small swings are ignored, big ones are eased
    toward (a few passes of fade instead of a chameleon flash). Boldness is
    a property of the glyphs, not the backdrop: it never re-flips.
    """
    if old is None:
        return fresh
    bg = _ease_color(old[0], fresh[0], _COLOR_CHANGE_MIN)
    fg = _ease_color(old[1], fresh[1], int(_COLOR_CHANGE_MIN * 1.5))
    return bg, fg, old[2]


def _ease_color(old: str, fresh: str, threshold: int) -> str:
    if _color_distance(old, fresh) <= threshold:
        return old
    try:
        a = [int(old[i : i + 2], 16) for i in (1, 3, 5)]
        b = [int(fresh[i : i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return fresh
    mixed = [round(x + (y - x) * _COLOR_EASE) for x, y in zip(a, b, strict=True)]
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def _color_distance(a: str, b: str) -> int:
    try:
        first = tuple(int(a[i : i + 2], 16) for i in (1, 3, 5))
        second = tuple(int(b[i : i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return 255 * 3
    return sum(abs(x - y) for x, y in zip(first, second, strict=True))


def _sticky_paint(
    paints: dict[tuple[int, int], _Paint],
    key: tuple[int, int],
    source: str,
    now: float,
) -> str | None:
    """Last translation painted at this spot, while it is still relevant.

    A busy background re-reads the same UI text slightly differently
    ("OYNAMAK İÇİN" vs "WI OYNAMAK İÇİN"), which is a fresh cache key and
    therefore "pending" — the item would vanish until the translation lands,
    which is exactly the flicker seen on the live game menu. Reuse the last
    painted text meanwhile, but only briefly and only while the source text
    is recognizably the same element.
    """
    remembered = paints.get(key)
    if remembered is None:
        return None
    if now - remembered.seen > _STICKY_PAINT_S:
        return None
    ratio = SequenceMatcher(None, _norm(remembered.source), _norm(source)).ratio()
    return remembered.display if ratio >= _STICKY_PAINT_SIMILARITY else None


def _preserve_outside_state(
    old: StabilizeState, new: StabilizeState, region: Rect, margin: int = 24
) -> None:
    """Copy damping entries whose bucket lies outside the refreshed region."""
    for name in ("votes", "misses", "births"):
        old_map = getattr(old, name)
        new_map = getattr(new, name)
        for key, value in old_map.items():
            x, y = key[0] * _MISS_BUCKET_PX, key[1] * _MISS_BUCKET_PX
            inside = (
                region.left - margin <= x <= region.right + margin
                and region.top - margin <= y <= region.bottom + margin
            )
            if not inside:
                new_map.setdefault(key, value)


def _pending_region(lines: list[OcrLine], stab: StabilizeState, frame: Image.Image) -> Rect | None:
    """Padded AABB of everything awaiting a follow-up OCR pass.

    Covers both ghost lines (kept alive by the TTL, present in ``lines``) and
    birth-held lines (not drawn yet, geometry carried inside the birth entry).
    """
    ghosts = [
        line
        for line in lines
        if (line.left // _MISS_BUCKET_PX, line.top // _MISS_BUCKET_PX) in stab.misses
        # A pending text vote ("Level 12" -> "Level 13" read once) needs
        # its confirming passes too, or a static HUD never updates.
        or (line.left // _MISS_BUCKET_PX, line.top // _MISS_BUCKET_PX) in stab.votes
    ]
    ghosts.extend(line for _count, line in stab.births.values())
    if not ghosts:
        return None
    pad = 16
    left = max(0, min(line.left for line in ghosts) - pad)
    top = max(0, min(line.top for line in ghosts) - pad)
    right = min(frame.width, max(line.left + line.width for line in ghosts) + pad)
    bottom = min(frame.height, max(line.top + line.height for line in ghosts) + pad)
    if right <= left or bottom <= top:
        return None
    return Rect(left, top, right - left, bottom - top)


def _line_vanished(
    frame: Image.Image, ink_marks: dict[tuple[int, int], tuple[str, float]], line: OcrLine
) -> bool:
    """Ghost check: is the text that was drawn at this line really gone?

    The blank test catches closed menus; the ink test catches a subtitle
    that left while the video behind it keeps moving: the share of pixels in
    the line's box painted in the text color collapses the frame it goes,
    whereas a background scrolling behind a still-present label barely moves
    it. Without this, a departed subtitle's cover survived the timed ghost
    TTL (measured 2.6 s) and sat on top of the NEXT subtitle.
    """
    if _region_blank(frame, line):
        return True
    mark = ink_marks.get(_sticky_key(line))
    if mark is None:
        return False
    fg, baseline = mark
    if baseline < _INK_MIN_BASELINE:
        return False
    return ink_fraction(frame, line, fg) < baseline * _INK_VANISH_RATIO


def _region_blank(frame: Image.Image, line: OcrLine) -> bool:
    """True when the line's box shows a near-uniform area on this frame.

    The ghost TTL keeps a line through an OCR dropout only while something is
    still drawn there; once the pixels go flat (menu closed, subtitle gone)
    the line must die immediately instead of lingering as a stale cover.
    """
    left = max(0, line.left)
    top = max(0, line.top)
    right = min(frame.width, line.left + line.width)
    bottom = min(frame.height, line.top + line.height)
    if right <= left or bottom <= top:
        return True
    crop = frame.crop((left, top, right, bottom)).convert("L")
    return ImageStat.Stat(crop).stddev[0] < _BLANK_STDDEV


def _drop_oversized_junk(lines: list[OcrLine]) -> list[OcrLine]:
    """Drop huge boxes carrying almost no letters (icon/logo misreads).

    An inventory icon misread as "HP" in a box over twice the screen's median
    line height would render as giant junk; genuine text that big carries
    more than a few letters.
    """
    median = median_line_height(lines)
    if median <= 0:
        return lines
    limit = median * _OVERSIZE_HEIGHT_FACTOR
    return [
        line
        for line in lines
        if line.height <= limit
        or sum(c.isalpha() for c in line.text) > _OVERSIZE_MAX_LETTERS
        # A big "Yes." / "Run!" subtitle over a page of small UI text is
        # real; the guard is for vowelless icon salad ("HP", "eøø").
        or is_probable_word(line.text)
    ]


def _color_key(line: OcrLine) -> tuple[int, int, str]:
    """Position-tolerant cache key for a line's picked colors (4 px buckets)."""
    return (line.left // 4, line.top // 4, line.text)


def _block_rep(block: list[OcrLine], text: str) -> OcrLine:
    """Representative line spanning a block's AABB, keyed by the merged text."""
    left = min(line.left for line in block)
    top = min(line.top for line in block)
    right = max(line.left + line.width for line in block)
    bottom = max(line.top + line.height for line in block)
    return OcrLine(text=text, left=left, top=top, width=right - left, height=bottom - top)


def _intersects(line: OcrLine, rect: Rect) -> bool:
    return not (
        line.left + line.width <= rect.left
        or rect.right <= line.left
        or line.top + line.height <= rect.top
        or rect.bottom <= line.top
    )


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()
