"""LocalTranslator tests on stubbed internals: no ctranslate2/sentencepiece.

The fake engine/sp pair mimics the real shapes (pieces are words, results
carry .hypotheses[0]) so sentence splitting, batching, and per-line
regrouping run through the genuine code paths.
"""

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import transcreen.local_mt as local_mt_module
from transcreen.config import config_dir
from transcreen.local_mt import (
    LocalMtError,
    LocalTranslator,
    default_model_dir,
    is_short_label,
    setup,
)

LONG_A = "The evening market opens after sunset."
LONG_B = "Local traders bring fresh bread every single day."


class FakeSp:
    """Pieces are just words; decode joins them back together."""

    def encode(self, text: str, out_type=str) -> list[str]:
        assert out_type is str  # the real call must ask for string pieces
        return text.split()

    def decode(self, pieces: list[str]) -> str:
        return " ".join(pieces)


class FakeEngine:
    """Prefixes each sentence with TR; records every batch call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[list[str]], dict]] = []

    def translate_batch(self, batch, **kwargs):
        self.calls.append(([list(tokens) for tokens in batch], kwargs))
        return [SimpleNamespace(hypotheses=[["TR", *tokens]]) for tokens in batch]


def _loaded_translator() -> tuple[LocalTranslator, FakeEngine]:
    translator = LocalTranslator(Path("unused-model-dir"))
    engine = FakeEngine()
    translator._engine = engine
    sp = FakeSp()
    translator._sp = sp
    translator._sp_target = sp  # Argos layout: one shared sentencepiece model
    return translator, engine


def test_translate_lines_splits_sentences_and_regroups_aligned():
    translator, engine = _loaded_translator()
    out = translator.translate_lines([f"{LONG_A} {LONG_B}", LONG_B])
    assert out == [f"TR {LONG_A} TR {LONG_B}", f"TR {LONG_B}"]
    assert len(engine.calls) == 1  # one batched inference for everything
    batch, kwargs = engine.calls[0]
    assert len(batch) == 3  # two sentences from line 1 + one from line 2
    assert kwargs == {
        "beam_size": 4,
        "max_decoding_length": 256,
        "max_batch_size": 16,
        "repetition_penalty": 1.1,
    }
    # Argos layout: no Marian EOS token is appended to the source pieces.
    assert all("</s>" not in tokens for tokens in batch)


def test_marian_layout_appends_eos_and_translates_short_labels():
    # The big opus-mt model loops without </s> and handles labels well, so
    # both behaviors flip on the Marian layout.
    translator, engine = _loaded_translator()
    translator._marian = True
    out = translator.translate_lines(["Settings", LONG_A])
    assert out[0] == "TR Settings </s>"  # short label translated, not skipped
    batch, _kwargs = engine.calls[0]
    assert all(tokens[-1] == "</s>" for tokens in batch)


def test_short_labels_and_empty_lines_come_back_blank():
    translator, engine = _loaded_translator()
    out = translator.translate_lines(["Settings", "Open inventory", "Go to bar", "", "   ", LONG_A])
    assert out[:5] == ["", "", "", "", ""]
    assert out[5] == f"TR {LONG_A}"
    # the skipped lines never reach the engine
    assert [len(batch) for batch, _kwargs in engine.calls] == [1]


def test_all_lines_skipped_returns_blanks_without_inference():
    translator, engine = _loaded_translator()
    assert translator.translate_lines(["Settings", ""]) == ["", ""]
    assert engine.calls == []


def test_not_loaded_returns_aligned_blanks():
    translator = LocalTranslator(Path("unused-model-dir"))
    assert translator.translate_lines([LONG_A, ""]) == ["", ""]


def test_engine_failure_returns_aligned_blanks():
    translator, engine = _loaded_translator()

    def boom(*_args, **_kwargs):
        raise RuntimeError("engine crashed")

    engine.translate_batch = boom  # type: ignore[method-assign]
    assert translator.translate_lines([LONG_A, LONG_B]) == ["", ""]


def test_monster_sentence_truncated_to_200_pieces():
    translator, engine = _loaded_translator()
    monster = ("word " * 300).strip()  # no terminator: one 300-piece sentence
    translator.translate_lines([monster])
    assert len(engine.calls[0][0][0]) == 200


def test_unavailable_latch_on_missing_model_dir(tmp_path):
    translator = LocalTranslator(tmp_path / "missing")
    translator._load()  # synchronous: the daemon thread runs exactly this
    assert not translator.available()
    assert translator._failed
    assert translator.translate_lines([LONG_A]) == [""]
    translator.start_loading()  # latched: must not spawn a loader thread
    assert translator._load_thread is None


def test_start_loading_noops_once_loaded():
    translator, _engine = _loaded_translator()
    translator.start_loading()
    assert translator._load_thread is None


def test_start_loading_thread_flips_available(monkeypatch):
    sp = FakeSp()
    monkeypatch.setattr(local_mt_module, "_load_engine", lambda _dir: (FakeEngine(), sp, sp, False))
    translator = LocalTranslator(Path("anywhere"))
    translator.start_loading()
    assert translator._load_thread is not None
    translator._load_thread.join(5.0)
    assert translator.available()


def test_default_model_dir_under_config():
    assert default_model_dir() == config_dir() / "models" / "en_tr"


def test_is_short_label_boundaries():
    assert is_short_label("Settings")  # one word
    assert is_short_label("Open inventory")  # two words
    assert is_short_label("go to bar")  # three words but only 9 chars
    assert not is_short_label("Inspect the package")  # 19 chars, 3 words


def test_setup_rejects_archive_without_model_and_cleans_up(tmp_path):
    bad = tmp_path / "bad.argosmodel"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("readme.txt", "not a model")
    target = tmp_path / "model_dir"
    with pytest.raises(LocalMtError):
        setup(target, source_file=bad, progress=lambda _msg: None)
    assert not target.exists()  # a failed install never leaves a directory


class _FakeDownload:
    """Stand-in for a streamed requests response (context manager + chunks)."""

    def __init__(self, size: int, chunks: int) -> None:
        self.headers = {"content-length": str(size)}
        self._chunks = [b"x" * (size // chunks)] * chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size):
        return iter(self._chunks)


def test_big_model_download_reports_percent_progress(tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda url, **kwargs: _FakeDownload(1000, 20))
    monkeypatch.setattr(local_mt_module, "_BIG_FILES", ("model.bin",))
    monkeypatch.setattr(local_mt_module, "_PROGRESS_MIN_BYTES", 500)
    messages: list[str] = []
    local_mt_module._download_big_model(tmp_path, messages.append)
    assert (tmp_path / "model.bin").read_bytes() == b"x" * 1000
    percents = [m for m in messages if "%" in m]
    # One line per 10 %, ending at 100 %: a long download never looks frozen.
    assert len(percents) == 10
    assert "%10 " in percents[0]
    assert "%100 " in percents[-1]


def test_small_download_files_skip_progress_lines(tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda url, **kwargs: _FakeDownload(100, 4))
    monkeypatch.setattr(local_mt_module, "_BIG_FILES", ("config.json",))
    monkeypatch.setattr(local_mt_module, "_PROGRESS_MIN_BYTES", 500)
    messages: list[str] = []
    local_mt_module._download_big_model(tmp_path, messages.append)
    assert not [m for m in messages if "%" in m]


def test_setup_missing_source_file_raises(tmp_path):
    target = tmp_path / "model_dir"
    with pytest.raises(LocalMtError):
        setup(target, source_file=tmp_path / "nope.argosmodel", progress=lambda _msg: None)
    assert not target.exists()
