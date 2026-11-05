"""GUI-less end-to-end check of the live pipeline, with a timing report."""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw, ImageFont

from .local_mt import LocalTranslator, default_model_dir
from .ocr import ParallelOcr, WindowsOcr
from .screen import grab_virtual_screen_fast
from .translator import Translator
from .vision import frame_signature


def _report_local_mt() -> None:
    """Report the offline en->tr tier: not installed, failed, or loaded + timed.

    Purely informational — the selftest never fails on the local tier, because
    running cloud-only is a fully supported configuration.
    """
    model_dir = default_model_dir()
    if not (model_dir.is_dir() and any(model_dir.rglob("model.bin"))):
        print(
            f"local MT:      model not installed ({model_dir}); "
            "install with: python -m transcreen --setup-local"
        )
        return
    local = LocalTranslator(model_dir)
    started = time.perf_counter()
    local.start_loading()
    deadline = started + 120.0
    # The loader runs on the "local-mt-load" daemon thread; a load failure
    # ends the thread without ever flipping available(), so watch both.
    while (
        not local.available()
        and time.perf_counter() < deadline
        and any(t.name == "local-mt-load" for t in threading.enumerate())
    ):
        time.sleep(0.05)
    if not local.available():
        print("local MT:      model present but failed to load (see app.log)")
        return
    load_s = time.perf_counter() - started
    batch = [
        "The quick brown fox jumps over the lazy dog.",
        "I didn't realise it was this late.",
        "Read the note on the table before you leave.",
    ]
    started = time.perf_counter()
    results = local.translate_lines(batch)
    batch_ms = (time.perf_counter() - started) * 1000
    for source, result in zip(batch, results, strict=True):
        print(f"  local {source!r} -> {result!r}")
    filled = sum(1 for result in results if result.strip())
    print(
        f"local MT:      loaded in {load_s:.1f} s, 3-line batch {batch_ms:.0f} ms "
        f"({filled}/{len(batch)} translated)"
    )


def _render_sample() -> Image.Image:
    img = Image.new("RGB", (960, 150), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype("segoeui.ttf", 32)
    draw.text((24, 24), "The quick brown fox jumps over", fill="black", font=font)
    draw.text((24, 76), "the lazy dog. It is a sunny day.", fill="black", font=font)
    return img


def run_selftest() -> int:
    print("== Transcreen selftest ==")
    try:
        languages = WindowsOcr.available_languages()
    except Exception as exc:  # winsdk missing or WinRT failure
        print(f"FAIL: Windows OCR unavailable: {exc}")
        return 1
    print(f"OCR languages: {', '.join(languages) or '(none)'}")

    ocr = WindowsOcr()
    lines = ocr.recognize_lines(_render_sample())
    print(f"OCR engine:    {ocr.language}")
    for line in lines:
        print(f"  line {line.text!r} @ ({line.left},{line.top}) {line.width}x{line.height}px")
    joined = " ".join(line.text for line in lines)
    if "fox" not in joined.lower():
        print("FAIL: OCR did not read the sample text")
        return 1
    if not all(line.width > 0 and line.height > 0 for line in lines):
        print("FAIL: OCR returned empty bounding boxes")
        return 1

    translator = Translator("tr")
    batch = ["File", "Edit", "The quick brown fox jumps over the lazy dog."]
    started = time.perf_counter()
    try:
        translated = translator.translate_lines(batch)
    except Exception as exc:
        print(f"FAIL: translation failed (no internet?): {exc}")
        return 1
    batch_ms = (time.perf_counter() - started) * 1000
    for source, result in zip(batch, translated, strict=True):
        print(f"  {source!r} -> {result!r}")
    if not all(t.strip() for t in translated):
        print("FAIL: empty translation in batch")
        return 1
    started = time.perf_counter()
    translator.translate_lines(batch)
    cached_ms = (time.perf_counter() - started) * 1000
    print(f"batch translate: {batch_ms:.0f} ms (1 request), cached rerun: {cached_ms:.2f} ms")

    _report_local_mt()

    # Real-screen timing: idle-loop cost (grab+signature), strip OCR, full OCR.
    frame = grab_virtual_screen_fast()  # warmup
    n = 10
    started = time.perf_counter()
    for _ in range(n):
        frame = grab_virtual_screen_fast()
    grab_ms = (time.perf_counter() - started) * 1000 / n
    started = time.perf_counter()
    for _ in range(n):
        frame_signature(frame)
    sig_ms = (time.perf_counter() - started) * 1000 / n

    strip = frame.crop((0, frame.height // 3, frame.width, frame.height // 3 + 140))
    started = time.perf_counter()
    strip_lines = ocr.recognize_lines(strip)
    strip_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    screen_lines = ocr.recognize_lines(frame)
    full_ms = (time.perf_counter() - started) * 1000

    parallel = ParallelOcr(workers=4)
    parallel.recognize_lines(frame)  # warm up the per-thread engines
    started = time.perf_counter()
    parallel_lines = parallel.recognize_lines(frame)
    parallel_ms = (time.perf_counter() - started) * 1000
    parallel.shutdown()

    print(
        f"real screen:   {frame.width}x{frame.height}px  grab {grab_ms:.1f} ms + "
        f"signature {sig_ms:.1f} ms (idle cycle ~{grab_ms + sig_ms:.0f} ms)"
    )
    print(
        f"OCR:           140px strip {strip_ms:.0f} ms ({len(strip_lines)} lines), "
        f"full frame {full_ms:.0f} ms ({len(screen_lines)} lines), "
        f"parallel full {parallel_ms:.0f} ms ({len(parallel_lines)} lines)"
    )
    print(
        "live cycle:    unchanged screen = idle cycle only; local change = strip OCR; "
        "translations render from cache instantly, misses arrive async"
    )
    print("SELFTEST OK")
    return 0
