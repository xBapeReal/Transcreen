"""Local en->tr machine translation: CTranslate2 + SentencePiece, no network.

Two model layouts are supported, detected from the files on disk:

* **Marian / Opus-MT tc-big** (`source.spm` + `target.spm`, the default since
  it is markedly more accurate): 476 MB, ~46 ms per warm line at beam 4, and
  it handles short UI labels correctly ("Store Manager" -> "Mağaza
  Yöneticisi"). Marian decoders REQUIRE the `</s>` end token appended to the
  source pieces; without it the model loops ("mağaza yönetici mağaza ...").
* **Argos OpenTech en_tr** (single `sentencepiece.model`): the older, smaller
  package (~130 MB, ~8 ms/line) kept working for existing installs. Its
  1-2 word labels are unreliable ("Settings" -> "Ayarlar Ayarları"), so short
  labels stay with the cloud tier on that layout only.

Either way this is the offline first-paint tier: text is painted in Turkish
within roughly one loop cycle and the cloud translator may upgrade it later.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import config_dir
from .translator import split_sentences

logger = logging.getLogger(__name__)

_INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
# links[0] of the index's en->tr entry, pinned as measured 2026-08 (124.7 MB);
# used directly when the index itself is unreachable.
_PINNED_MODEL_URL = "https://argos-net.com/v1/translate-en_tr-1_5.argosmodel"

# Prebuilt CTranslate2 build of Helsinki-NLP/opus-mt-tc-big-en-tr (476 MB).
_BIG_REPO = "ooeoeo/opus-mt-tc-big-en-tr-ct2-float16"
_BIG_FILES = ("model.bin", "config.json", "shared_vocabulary.json", "source.spm", "target.spm")
_HF_FILE_URL = "https://huggingface.co/{repo}/resolve/main/{name}"
_PROGRESS_MIN_BYTES = 8 * 1024 * 1024  # only big downloads print % progress
_MARIAN_EOS = "</s>"  # Marian decoders loop without it

_MIN_MODEL_BYTES = 20 * 1024 * 1024  # a real model.bin is ~130 MB
_MAX_SP_PIECES = 200  # truncate monster sentences; keeps decoding bounded
_BEAM_SIZE = 4  # beam 2 was measurably sloppier on game text
_MAX_DECODING_LENGTH = 256
_MAX_BATCH_SIZE = 16
_PROBE_TEXT = "Hello world"

# Measured hazard: the model doubles/derails 1-2 word labels, so anything
# this short stays with the cloud tier.
_SHORT_LABEL_MAX_WORDS = 2
_SHORT_LABEL_MAX_CHARS = 12


class LocalMtError(RuntimeError):
    """Raised when the local model cannot be set up or verified."""


def default_model_dir() -> Path:
    """Where the en->tr model lives: %APPDATA%/Transcreen/models/en_tr."""
    return config_dir() / "models" / "en_tr"


def is_short_label(text: str) -> bool:
    """True for 1-2 word or <=12 char snippets the local model mistranslates."""
    stripped = " ".join(text.split())
    if len(stripped) <= _SHORT_LABEL_MAX_CHARS:
        return True
    return len(stripped.split()) <= _SHORT_LABEL_MAX_WORDS


class LocalTranslator:
    """Loads the CT2 model on a daemon thread and batch-translates lines."""

    def __init__(self, model_dir: Path | str | None = None) -> None:
        self._model_dir = Path(model_dir) if model_dir is not None else default_model_dir()
        self._lock = threading.Lock()
        self._engine: Any | None = None  # ctranslate2.Translator
        self._sp: Any | None = None  # source SentencePieceProcessor
        self._sp_target: Any | None = None  # target sp (Marian) or the same one
        self._marian = False  # Marian layout: append EOS, trust short labels
        self._failed = False
        self._load_thread: threading.Thread | None = None

    def start_loading(self) -> None:
        """Begin the ~6 s cold load on a daemon thread; safe to call again."""
        with self._lock:
            if self._engine is not None or self._failed or self._load_thread is not None:
                return
            thread = threading.Thread(target=self._load, name="local-mt-load", daemon=True)
            self._load_thread = thread
        thread.start()

    def available(self) -> bool:
        with self._lock:
            return self._engine is not None and self._sp is not None

    def _load(self) -> None:
        try:
            engine, sp, sp_target, marian = _load_engine(self._model_dir)
        except Exception as exc:
            with self._lock:
                self._failed = True
            # One line, no traceback: a missing model or wheel simply means
            # "stay cloud-only", exactly today's behavior.
            logger.info(
                "local MT unavailable (%s: %s), staying cloud-only", type(exc).__name__, exc
            )
            return
        with self._lock:
            self._engine = engine
            self._sp = sp
            self._sp_target = sp_target
            self._marian = marian
        logger.info(
            "local MT ready (%s): %s", "opus-mt-tc-big" if marian else "argos", self._model_dir
        )

    def translate_lines(self, lines: list[str]) -> list[str]:
        """Translate lines en->tr; the output aligns with the input.

        A slot comes back "" when its line is empty, is a short label (left
        to the cloud tier on measured quality grounds), the model is not
        loaded yet, or inference failed. Every line is split into sentences,
        all sentences run through ONE translate_batch call, and the decoded
        pieces are regrouped per line.
        """
        with self._lock:
            engine, sp = self._engine, self._sp
            sp_target, marian = self._sp_target or self._sp, self._marian
        if engine is None or sp is None or sp_target is None:
            return ["" for _ in lines]
        counts: list[int] = []
        batch: list[list[str]] = []
        for line in lines:
            text = " ".join(line.split())
            # The big Marian model handles short labels correctly; the small
            # Argos one derails on them, so only that layout defers to cloud.
            if not text or (not marian and is_short_label(text)):
                counts.append(0)
                continue
            encoded: list[list[str]] = []
            for token in split_sentences(text):
                sentence = token.strip()
                if not sentence:
                    continue
                pieces = list(sp.encode(sentence, out_type=str))[:_MAX_SP_PIECES]
                if marian:
                    pieces.append(_MARIAN_EOS)
                encoded.append(pieces)
            counts.append(len(encoded))
            batch.extend(encoded)
        if not batch:
            return ["" for _ in lines]
        try:
            results = engine.translate_batch(
                batch,
                beam_size=_BEAM_SIZE,
                max_decoding_length=_MAX_DECODING_LENGTH,
                max_batch_size=_MAX_BATCH_SIZE,
                repetition_penalty=1.1,
            )
            decoded = [str(sp_target.decode(result.hypotheses[0])) for result in results]
        except Exception:
            logger.warning("local MT batch failed", exc_info=True)
            return ["" for _ in lines]
        outputs: list[str] = []
        cursor = 0
        for count in counts:
            parts = [part.strip() for part in decoded[cursor : cursor + count]]
            cursor += count
            outputs.append(" ".join(part for part in parts if part))
        return outputs


# ------------------------------------------------------------------- setup
def _locate_model_files(model_dir: Path) -> tuple[Path, Path, Path, bool]:
    """Find (ct2 dir, source sp, target sp, is_marian) inside a model dir.

    Marian/Opus-MT packages ship source.spm + target.spm; the Argos package
    ships one shared sentencepiece.model.
    """
    if not model_dir.is_dir():
        raise LocalMtError(f"model directory missing: {model_dir}")
    bins = sorted(model_dir.rglob("model.bin"))
    if not bins:
        raise LocalMtError(f"no model.bin under {model_dir}")
    ct2_dir = bins[0].parent
    source = next((p for p in ct2_dir.rglob("source.spm")), None)
    target = next((p for p in ct2_dir.rglob("target.spm")), None)
    if source is not None and target is not None:
        return ct2_dir, source, target, True
    candidates = [p for p in model_dir.rglob("*.model") if "sentencepiece" in p.name.lower()]
    if not candidates:
        candidates = list(model_dir.rglob("*.spm"))
    if not candidates:
        raise LocalMtError(f"no sentencepiece model under {model_dir}")
    shared = sorted(candidates)[0]
    return ct2_dir, shared, shared, False


def _load_engine(model_dir: Path) -> tuple[Any, Any, Any, bool]:
    """Load ct2 + sentencepiece for model_dir (lazy imports: heavy, optional)."""
    ct2_dir, sp_path, sp_target_path, marian = _locate_model_files(model_dir)
    import ctranslate2
    import sentencepiece

    sp = sentencepiece.SentencePieceProcessor(model_file=str(sp_path))
    sp_target = (
        sp
        if sp_target_path == sp_path
        else sentencepiece.SentencePieceProcessor(model_file=str(sp_target_path))
    )
    # CPU on purpose: it is fast enough per line, and CUDA inference with this
    # wheel proved unreliable in testing (loads fine, then 0/N translated).
    engine = ctranslate2.Translator(
        str(ct2_dir),
        device="cpu",
        compute_type="int8",  # the big model ships float16; int8 runs on CPU
        inter_threads=1,
        intra_threads=max(1, (os.cpu_count() or 2) // 2),
    )
    return engine, sp, sp_target, marian


def _download_model(progress: Callable[[str], None]) -> bytes:
    """Fetch the .argosmodel zip: index first, pinned URL as the fallback."""
    import requests

    url = _PINNED_MODEL_URL
    try:
        index = requests.get(_INDEX_URL, timeout=30).json()
        entry = next(
            item
            for item in index
            if isinstance(item, dict)
            and item.get("from_code") == "en"
            and item.get("to_code") == "tr"
        )
        links = entry.get("links") or []
        if links and isinstance(links[0], str):
            url = links[0]
    except Exception:
        logger.info("argos index unreachable, falling back to the pinned model URL")
    progress(f"Kaynak: {url}")
    response = requests.get(url, timeout=600)
    response.raise_for_status()
    return response.content


def _verify(model_dir: Path) -> None:
    """Raise LocalMtError unless model_dir holds a genuinely working model."""
    ct2_dir, _sp_path, _sp_target_path, _marian = _locate_model_files(model_dir)
    size = (ct2_dir / "model.bin").stat().st_size
    if size < _MIN_MODEL_BYTES:
        raise LocalMtError(f"model.bin is implausibly small ({size} bytes)")
    engine, sp, sp_target, marian = _load_engine(model_dir)
    pieces = list(sp.encode(_PROBE_TEXT, out_type=str))
    if marian:
        pieces.append(_MARIAN_EOS)
    results = engine.translate_batch(
        [pieces], beam_size=_BEAM_SIZE, max_decoding_length=_MAX_DECODING_LENGTH
    )
    probe = str(sp_target.decode(results[0].hypotheses[0]))
    if not probe.strip():
        raise LocalMtError("probe translation came back empty")
    # A Marian model without its EOS token loops; catch that here rather than
    # shipping a model that produces "mağaza yönetici mağaza yönetici ...".
    words = probe.split()
    if len(words) > 6 and len(set(words)) <= len(words) // 3:
        raise LocalMtError(f"probe translation degenerated into a loop: {probe[:60]!r}")
    logger.info("local model verified: %r -> %r", _PROBE_TEXT, probe)


def _verify_quietly(model_dir: Path) -> bool:
    try:
        _verify(model_dir)
    except Exception:
        return False
    return True


def _download_big_model(target: Path, progress: Callable[[str], None]) -> None:
    """Fetch the prebuilt opus-mt-tc-big CT2 files straight from the HF CDN."""
    import requests

    target.mkdir(parents=True, exist_ok=True)
    for name in _BIG_FILES:
        url = _HF_FILE_URL.format(repo=_BIG_REPO, name=name)
        progress(f"İndiriliyor: {name}")
        with requests.get(url, timeout=900, stream=True) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            report = total >= _PROGRESS_MIN_BYTES
            done = 0
            next_pct = 10
            with (target / name).open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    handle.write(chunk)
                    done += len(chunk)
                    # A 476 MB file takes minutes: report every 10 % so the
                    # launcher console never looks frozen.
                    while report and next_pct <= 100 and done * 100 >= total * next_pct:
                        progress(f"  {name}: %{next_pct} ({done >> 20}/{total >> 20} MB)")
                        next_pct += 10


def setup(
    model_dir: Path | str | None = None,
    source_file: Path | str | None = None,
    progress: Callable[[str], None] = print,
    small: bool = False,
) -> Path:
    """Install and verify the en->tr model; returns the model directory.

    By default this installs the accurate opus-mt-tc-big CT2 build (476 MB).
    ``small=True`` (or passing a .argosmodel via source_file) installs the
    older, lighter Argos package instead. Either way the result is verified
    end to end: a plausible model.bin size, a real engine load, a probe
    translation and a degeneration check. Any failure removes model_dir so a
    half-installed model can never be picked up, then raises LocalMtError.
    """
    target = Path(model_dir) if model_dir is not None else default_model_dir()
    if source_file is None and _verify_quietly(target):
        progress(f"Lokal model zaten kurulu: {target}")
        return target
    try:
        if source_file is not None:
            progress(f"Paket dosyadan okunuyor: {source_file}")
            blob = Path(source_file).read_bytes()
            target.mkdir(parents=True, exist_ok=True)
            progress("Arşiv açılıyor...")
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                archive.extractall(target)
        elif small:
            progress("Küçük (Argos) model indiriliyor (~125 MB)...")
            blob = _download_model(progress)
            target.mkdir(parents=True, exist_ok=True)
            progress("Arşiv açılıyor...")
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                archive.extractall(target)
        else:
            progress("Doğruluğu yüksek model indiriliyor (opus-mt-tc-big, ~476 MB)...")
            _download_big_model(target, progress)
        progress("Model doğrulanıyor (yükleme + deneme çevirisi)...")
        _verify(target)
    except Exception as exc:
        shutil.rmtree(target, ignore_errors=True)
        if isinstance(exc, LocalMtError):
            raise
        raise LocalMtError(f"local model setup failed: {exc}") from exc
    progress(f"Lokal model hazır: {target}")
    return target
