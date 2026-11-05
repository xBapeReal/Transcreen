import json
import threading
import time
from types import SimpleNamespace

import pytest

import transcreen.translator as translator_module
from transcreen.translator import (
    BackgroundTranslator,
    TranslationError,
    Translator,
    _GtxSorryRedirectError,
    _is_gtx_rate_limited,
    _mymemory_language,
    _mymemory_translate,
    chunk_text,
    gtx_available,
    gtx_join_segments,
    is_translatable,
    looks_like_text,
    pacing_delay,
    split_sentences,
)


def test_chunk_short_passthrough():
    assert chunk_text("hello") == ["hello"]


def test_chunk_partition_is_lossless():
    text = ("Sentence one. " * 400) + "tail without punct"
    chunks = chunk_text(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(chunks) == text


def test_chunk_prefers_sentence_boundaries():
    text = ("One. Two. Three. " * 100).strip()
    chunks = chunk_text(text, limit=100)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk.rstrip().endswith(".")


def test_chunk_hard_cuts_monster_token():
    text = "x" * 950
    chunks = chunk_text(text, limit=300)
    assert "".join(chunks) == text
    assert max(len(c) for c in chunks) <= 300


def test_translate_uses_backend_and_caches_whitespace_jitter():
    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return f"TR<{text}>"

    translator = Translator("tr", backend=fake)
    first = translator.translate("Hello world")
    second = translator.translate("Hello   world")  # OCR jitter, must hit the cache
    assert first == second == "TR<Hello world>"
    assert len(calls) == 1


def test_translate_empty_never_calls_backend():
    def boom(_text: str) -> str:
        raise AssertionError("backend must not be called")

    assert Translator(backend=boom).translate("   \n ") == ""


def test_translate_wraps_backend_errors():
    def broken(_text: str) -> str:
        raise ValueError("boom")

    with pytest.raises(TranslationError):
        Translator(backend=broken).translate("hello")


def test_target_change_misses_cache():
    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return text.upper()

    translator = Translator("tr", backend=fake)
    translator.translate("hi")
    translator.set_target("de")
    translator.translate("hi")
    assert len(calls) == 2


def test_cache_eviction_keeps_size_bounded():
    translator = Translator("tr", backend=str.upper, cache_size=2)
    for word in ("a", "b", "c", "d"):
        translator.translate(word)
    assert len(translator._cache) == 2


def test_translate_lines_batches_unique_lines_in_one_request():
    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return "\n".join(f"TR:{part}" for part in text.split("\n"))

    translator = Translator("tr", backend=fake)
    out = translator.translate_lines(["File", "Edit", "File", ""])
    assert out == ["TR:File", "TR:Edit", "TR:File", ""]
    assert len(calls) == 1  # deduplicated, newline-joined single request


def test_translate_lines_second_call_is_all_cache():
    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return "\n".join(f"TR:{part}" for part in text.split("\n"))

    translator = Translator("tr", backend=fake)
    translator.translate_lines(["File", "Edit"])
    out = translator.translate_lines(["Edit", "File"])
    assert out == ["TR:Edit", "TR:File"]
    assert len(calls) == 1


def test_translate_lines_falls_back_when_service_merges_lines():
    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        if "\n" in text:
            return "everything merged into one line"
        return f"TR:{text}"

    translator = Translator("tr", backend=fake)
    out = translator.translate_lines(["Alpha", "Beta"])
    assert out == ["TR:Alpha", "TR:Beta"]
    assert len(calls) == 3  # 1 failed batch + 2 individual retries


def test_translate_lines_error_propagates():
    def broken(_text: str) -> str:
        raise ValueError("boom")

    with pytest.raises(TranslationError):
        Translator("tr", backend=broken).translate_lines(["hello"])


def test_translate_lines_retries_empty_batch_parts_individually():
    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        if "\n" in text:  # batch: right line count, but one part comes back empty
            return "\n".join("" if part == "Beta" else f"TR:{part}" for part in text.split("\n"))
        return f"IND:{text}"

    translator = Translator("tr", backend=fake)
    out = translator.translate_lines(["Alpha", "Beta", "Gamma"])
    assert out == ["TR:Alpha", "IND:Beta", "TR:Gamma"]
    assert calls == ["Alpha\nBeta\nGamma", "Beta"]  # only the empty part was retried
    # the healthy parts were cached straight from the batch response
    assert translator.try_cached(["Alpha", "Gamma"]) == ["TR:Alpha", "TR:Gamma"]


def test_empty_translation_returned_uncached_and_retried_later():
    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return ""  # backend yields nothing, for batches and singles alike

    translator = Translator("tr", backend=fake)
    assert translator.translate_lines(["Hello"]) == [""]
    assert translator.try_cached(["Hello"]) == [None]  # a miss, not "translated as nothing"
    before = len(calls)
    translator.translate_lines(["Hello"])
    assert len(calls) > before  # a later call asks the backend again


def test_concurrent_fallback_preserves_input_order():
    def fake(text: str) -> str:
        if "\n" in text:
            return "merged into a single line"  # force the per-line fallback
        return f"TR:{text}"

    translator = Translator("tr", backend=fake)
    lines = [f"Line {i}" for i in range(12)]
    assert translator.translate_lines(lines) == [f"TR:Line {i}" for i in range(12)]


def test_concurrent_fallback_propagates_worker_errors():
    def fake(text: str) -> str:
        if "\n" in text:
            return "merged"  # force the per-line fallback
        raise ValueError("boom")

    with pytest.raises(TranslationError):
        Translator("tr", backend=fake).translate_lines(["Alpha", "Beta"])


def test_is_translatable():
    assert is_translatable("Hello")
    assert is_translatable("çöğüşıİ")
    assert not is_translatable("12345")
    assert not is_translatable("+ - * / % 42")
    assert not is_translatable("   ")


def test_looks_like_text_accepts_real_words():
    assert looks_like_text("Hello")
    assert looks_like_text("İki kelime")
    assert looks_like_text("ok")
    assert looks_like_text("USB")  # short acronyms pass


def test_looks_like_text_rejects_ocr_junk():
    assert not looks_like_text("vwWl")  # 4+ letters, no vowel: misread
    assert not looks_like_text("—|—")  # no letters
    assert not looks_like_text("A1")  # single letter
    assert not looks_like_text("22:30")
    assert not looks_like_text("x=[1](2)")  # letters drowned in symbols


def _echo_backend(text: str) -> str:
    return "\n".join(f"X{part}" for part in text.split("\n"))


def test_try_cached_is_network_free():
    translator = Translator("tr", backend=_echo_backend)
    assert translator.try_cached(["Hi", " "]) == [None, ""]
    translator.translate_lines(["Hi"])
    assert translator.try_cached(["Hi"]) == ["XHi"]


def test_translate_lines_fetches_multiple_groups_concurrently():
    calls: list[str] = []
    lock = threading.Lock()

    def fake(text: str) -> str:
        with lock:
            calls.append(text)
        return "\n".join(f"TR:{part}" for part in text.split("\n"))

    translator = Translator("tr", backend=fake, batch_limit=12)
    lines = [f"line{i:02d}" for i in range(6)]  # 7 chars each -> one line per group
    assert translator.translate_lines(lines) == [f"TR:{line}" for line in lines]
    assert len(calls) == 6  # every group fetched (concurrently), none dropped


def test_gtx_join_segments_concatenates_translations():
    payload = [[["Merhaba ", "Hello ", None], ["dünya", "world", None]], None, "en"]
    assert gtx_join_segments(payload) == "Merhaba dünya"


def test_gtx_join_segments_rejects_garbage():
    with pytest.raises(ValueError):
        gtx_join_segments({"not": "a list"})
    with pytest.raises(ValueError):
        gtx_join_segments([])


def test_cache_persists_across_instances(tmp_path):
    path = tmp_path / "cache.json"
    first = Translator("tr", backend=_echo_backend)
    first.enable_persistence(path)
    first.translate_lines(["Hi", "There"])
    first.save_cache()

    def boom(_text: str) -> str:
        raise AssertionError("backend must not be called")

    second = Translator("tr", backend=boom)
    second.enable_persistence(path)
    assert second.try_cached(["Hi", "There"]) == ["XHi", "XThere"]


def test_enable_persistence_drops_poisoned_empty_entries(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps({"tr": {"Poisoned": "", "Blank": "   ", "Fine": "XFine"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    translator = Translator("tr", backend=_echo_backend)
    translator.enable_persistence(path)
    # empty/whitespace disk values are purged: those lines read as misses again
    assert translator.try_cached(["Poisoned", "Blank", "Fine"]) == [None, None, "XFine"]


def test_enable_persistence_drops_untrusted_echo_entries(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps(
            {
                "tr": {
                    "The evening market opens": "The evening market opens",  # echo poison
                    "Zaten Türkçe çğü": "Zaten Türkçe çğü",  # legit Turkish echo
                    "Hello": "Merhaba",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    translator = Translator("tr", backend=_echo_backend)
    translator.enable_persistence(path)
    assert translator.try_cached(["The evening market opens"]) == [None]
    assert translator.try_cached(["Zaten Türkçe çğü"]) == ["Zaten Türkçe çğü"]
    assert translator.try_cached(["Hello"]) == ["Merhaba"]


def test_trusted_backend_echo_is_cached():
    translator = Translator("tr", backend=lambda text: text)  # injected = trusted
    translator.translate_lines(["Zaten Türkçe metin"])
    assert translator.try_cached(["Zaten Türkçe metin"]) == ["Zaten Türkçe metin"]


def test_repeated_untrusted_echoes_accepted_as_untranslatable():
    translator = Translator("tr")
    # Two untrusted echoes stay uncached (fallback bounce), the third is
    # accepted: a text that consistently echoes is a proper noun, and caching
    # it stops the endless retry loop while gtx is rate-limited.
    assert translator._store_result("Starfall", "Starfall", trusted=False) is None
    assert translator.try_cached(["Starfall"]) == [None]
    assert translator._store_result("Starfall", "Starfall", trusted=False) is None
    assert translator._store_result("Starfall", "Starfall", trusted=False) == "Starfall"
    assert translator.try_cached(["Starfall"]) == ["Starfall"]


def test_accepted_echo_is_session_only_never_persisted(tmp_path):
    path = tmp_path / "cache.json"
    translator = Translator("tr")
    translator.enable_persistence(path)
    for _ in range(3):
        translator._store_result("Some english sentence", "Some english sentence", trusted=False)
    # Served from cache within the session (stops the retry loop)...
    assert translator.try_cached(["Some english sentence"]) == ["Some english sentence"]
    translator.save_cache()
    raw = json.loads(path.read_text(encoding="utf-8"))
    # ...but never fossilized on disk: the next start retries it for real.
    assert "Some english sentence" not in raw.get("tr", {})


def test_trusted_result_clears_echo_strikes():
    translator = Translator("tr")
    translator._store_result("Portal", "Portal", trusted=False)
    translator._store_result("Portal", "Portal", trusted=False)
    translator._store_result("Portal", "Geçit", trusted=True)  # gtx recovered
    assert translator.try_cached(["Portal"]) == ["Geçit"]
    # Strike count restarted: a later untrusted echo is once again rejected.
    assert translator._store_result("Portal2", "Portal2", trusted=False) is None


def test_pacing_delay_first_fetch_is_immediate():
    assert pacing_delay(float("-inf"), 0.0, 0.2) == 0.0


def test_pacing_delay_waits_out_the_remainder():
    assert pacing_delay(10.0, 10.05, 0.2) == pytest.approx(0.15)


def test_pacing_delay_zero_once_interval_elapsed():
    assert pacing_delay(10.0, 10.2, 0.2) == 0.0
    assert pacing_delay(10.0, 99.0, 0.2) == 0.0  # never negative


def _http_error(status: int | None = None, url: str = "") -> RuntimeError:
    exc = RuntimeError("http failure")
    exc.response = SimpleNamespace(status_code=status, url=url)  # type: ignore[attr-defined]
    return exc


def test_rate_limit_detection_on_status_codes():
    assert _is_gtx_rate_limited(_http_error(status=429))
    assert _is_gtx_rate_limited(_http_error(status=403))
    assert not _is_gtx_rate_limited(_http_error(status=500))


def test_rate_limit_detection_on_sorry_redirect():
    sorry = "https://www.google.com/sorry/index?continue=x"
    assert _is_gtx_rate_limited(_http_error(status=200, url=sorry))
    assert _is_gtx_rate_limited(_GtxSorryRedirectError("redirected"))
    assert not _is_gtx_rate_limited(ValueError("no response attached"))


def test_mymemory_language_maps_short_codes():
    assert _mymemory_language("tr") == "tr-TR"
    assert _mymemory_language("tr-TR") == "tr-TR"
    assert _mymemory_language("xx") is None


def test_mymemory_translate_rejects_oversized_payload():
    with pytest.raises(ValueError):
        _mymemory_translate("x" * 501, "tr")


def test_mymemory_translate_uses_autodetect_and_region_code(monkeypatch):
    created: dict[str, str] = {}

    class FakeMyMemory:
        def __init__(self, source: str, target: str) -> None:
            created["target"] = target
            self.source = source

        def translate(self, text: str) -> str:
            created["source"] = self.source
            return f"MM:{text}"

    monkeypatch.setattr("deep_translator.MyMemoryTranslator", FakeMyMemory)
    assert _mymemory_translate("Hello", "tr") == "MM:Hello"
    assert created == {"target": "tr-TR", "source": "Autodetect"}


def test_mymemory_translate_rejects_error_banner(monkeypatch):
    class FakeMyMemory:
        def __init__(self, source: str, target: str) -> None:
            self.source = source

        def translate(self, text: str) -> str:
            return "MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS FOR TODAY"

    monkeypatch.setattr("deep_translator.MyMemoryTranslator", FakeMyMemory)
    with pytest.raises(ValueError):
        _mymemory_translate("Hello", "tr")


def _gtx_down(_text: str, _target: str) -> str:
    raise RuntimeError("gtx down (test stub)")


class _FakeGoogle:
    def __init__(self, source: str, target: str) -> None:
        pass

    def translate(self, text: str) -> str:
        return f"G:{text}"


def test_fallback_chain_second_candidate_serves_when_first_raises(monkeypatch):
    monkeypatch.setattr(translator_module, "_gtx_block_until", 0.0)
    monkeypatch.setattr(translator_module, "_gtx_translate", _gtx_down)

    def mymemory_down(_text: str, _target: str) -> str:
        raise RuntimeError("mymemory down (test stub)")

    monkeypatch.setattr(translator_module, "_mymemory_translate", mymemory_down)
    monkeypatch.setattr("deep_translator.GoogleTranslator", _FakeGoogle)

    translator = Translator("tr")  # no injected backend: the real chain runs
    assert translator.translate("Hello") == "G:Hello"


def test_gtx_429_starts_cooldown_and_chain_serves(monkeypatch):
    monkeypatch.setattr(translator_module, "_gtx_block_until", 0.0)
    gtx_calls: list[str] = []

    def gtx_limited(text: str, _target: str) -> str:
        gtx_calls.append(text)
        raise _http_error(status=429)

    monkeypatch.setattr(translator_module, "_gtx_translate", gtx_limited)
    monkeypatch.setattr(translator_module, "_mymemory_translate", lambda text, _t: f"MM:{text}")

    translator = Translator("tr")
    assert translator.translate("Hello") == "MM:Hello"
    assert translator.translate("World") == "MM:World"
    assert len(gtx_calls) == 1  # cooldown active: gtx skipped on the second call
    assert translator_module._gtx_block_until > time.monotonic()


def test_chain_echo_stays_uncached(monkeypatch):
    monkeypatch.setattr(translator_module, "_gtx_block_until", 0.0)
    monkeypatch.setattr(translator_module, "_gtx_translate", _gtx_down)
    monkeypatch.setattr(translator_module, "_mymemory_translate", lambda text, _t: text)

    class EchoGoogle:
        def __init__(self, source: str, target: str) -> None:
            pass

        def translate(self, text: str) -> str:
            return text

    monkeypatch.setattr("deep_translator.GoogleTranslator", EchoGoogle)

    translator = Translator("tr")
    assert translator.translate_lines(["Hello there"]) == [""]  # echo dropped
    assert translator.try_cached(["Hello there"]) == [None]  # stays a miss, retried later


def test_chain_skips_mymemory_over_its_char_limit(monkeypatch):
    monkeypatch.setattr(translator_module, "_gtx_block_until", 0.0)
    monkeypatch.setattr(translator_module, "_gtx_translate", _gtx_down)
    mymemory_calls: list[str] = []

    def fake_mymemory(text: str, _target: str) -> str:
        mymemory_calls.append(text)
        return f"MM:{text}"

    monkeypatch.setattr(translator_module, "_mymemory_translate", fake_mymemory)
    monkeypatch.setattr("deep_translator.GoogleTranslator", _FakeGoogle)

    long_line = "word " * 120  # 600 chars, over MyMemory's 500-char limit
    translator = Translator("tr")
    assert translator.translate(long_line).startswith("G:")
    assert mymemory_calls == []


def test_background_translator_fills_cache_and_signals():
    ready = threading.Event()
    translator = Translator("tr", backend=_echo_backend)
    background = BackgroundTranslator(translator, on_ready=ready.set, on_error=lambda _m: None)
    background.start()
    background.request(["Hello", "World"])
    assert ready.wait(3.0), "on_ready was not called"
    assert translator.try_cached(["Hello", "World"]) == ["XHello", "XWorld"]
    background.stop()
    background.join(2.0)
    assert not background.is_alive()


# ----------------------------------------------------------- local MT tier
class _StubLocal:
    """Duck-typed local backend: prefixes lines with L:, records calls."""

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.calls: list[list[str]] = []

    def available(self) -> bool:
        return self.up

    def translate_lines(self, lines: list[str]) -> list[str]:
        self.calls.append(list(lines))
        return [f"L:{line}" for line in lines]


def test_split_sentences_partitions_exactly():
    text = "One. Two!\nThree without end"
    parts = split_sentences(text)
    assert "".join(parts) == text
    assert parts[0] == "One."


def test_gtx_available_reflects_cooldown(monkeypatch):
    monkeypatch.setattr(translator_module, "_gtx_block_until", 0.0)
    assert gtx_available()
    monkeypatch.setattr(translator_module, "_gtx_block_until", time.monotonic() + 30.0)
    assert not gtx_available()


def test_prefill_local_stores_provisional_tier():
    local = _StubLocal()
    translator = Translator("tr", backend=_echo_backend, local=local)
    assert translator.prefill_local(["Hello world", "Zaten çevrilmiş", ""]) is True
    assert translator.try_cached(["Hello world"]) == ["L:Hello world"]
    assert local.calls == [["Hello world"]]  # Turkish + empty lines never sent
    assert translator.pending_upgrade(["Hello world"]) == ["Hello world"]


def test_prefill_local_noop_when_absent_or_loading():
    translator = Translator("tr", backend=_echo_backend)  # no local tier wired
    assert translator.prefill_local(["Hello"]) is False
    loading = _StubLocal(up=False)
    translator2 = Translator("tr", backend=_echo_backend, local=loading)
    assert translator2.prefill_local(["Hello"]) is False
    assert loading.calls == []


def test_prefill_local_skips_keys_with_any_entry():
    local = _StubLocal()
    translator = Translator("tr", backend=_echo_backend, local=local)
    translator.translate_lines(["Hello"])  # a cloud entry already exists
    assert translator.prefill_local(["Hello"]) is False
    assert translator.try_cached(["Hello"]) == ["XHello"]
    assert local.calls == []


def test_cloud_result_promotes_local_entry():
    local = _StubLocal()
    translator = Translator("tr", backend=_echo_backend, local=local)
    translator.prefill_local(["Hello world"])
    # The local-tier hit counts as a miss: the trusted backend is called and
    # its result replaces + promotes the entry.
    assert translator.translate_lines(["Hello world"]) == ["XHello world"]
    assert translator.try_cached(["Hello world"]) == ["XHello world"]
    assert translator.pending_upgrade(["Hello world"]) == []


def test_accepted_echo_does_not_clobber_local_value():
    local = _StubLocal()
    translator = Translator("tr", local=local)
    translator.prefill_local(["Starfall Chronicles"])
    for _ in range(2):
        assert (
            translator._store_result("Starfall Chronicles", "Starfall Chronicles", trusted=False)
            is None
        )
    kept = translator._store_result("Starfall Chronicles", "Starfall Chronicles", trusted=False)
    assert kept == "L:Starfall Chronicles"  # echo accepted, but the local text wins
    assert translator.try_cached(["Starfall Chronicles"]) == ["L:Starfall Chronicles"]
    # still local-tier: a later cloud success may yet upgrade it
    assert translator.pending_upgrade(["Starfall Chronicles"]) == ["Starfall Chronicles"]


def test_pending_upgrade_lists_only_local_tier_lines():
    local = _StubLocal()
    translator = Translator("tr", backend=_echo_backend, local=local)
    translator.prefill_local(["Alpha line", "Beta line"])
    translator.translate_lines(["Beta line"])  # promoted by the cloud
    pending = translator.pending_upgrade(["Alpha line", "Beta line", "Alpha line", "New", ""])
    assert pending == ["Alpha line"]


def test_local_tier_persists_in_its_own_section(tmp_path):
    path = tmp_path / "cache.json"
    local = _StubLocal()
    first = Translator("tr", backend=_echo_backend, local=local)
    first.enable_persistence(path)
    first.prefill_local(["Provisional line"])
    first.translate_lines(["Cloud line"])
    first.save_cache()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["__local__"]["tr"] == {"Provisional line": "L:Provisional line"}
    assert raw["tr"] == {"Cloud line": "XCloud line"}

    second = Translator("tr", backend=_echo_backend)
    second.enable_persistence(path)
    assert second.try_cached(["Provisional line"]) == ["L:Provisional line"]
    assert second.pending_upgrade(["Provisional line", "Cloud line"]) == ["Provisional line"]


def test_persisted_cloud_entry_wins_collision_with_local_section(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps(
            {"__local__": {"tr": {"Hello": "Yerel Merhaba"}}, "tr": {"Hello": "Merhaba"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    translator = Translator("tr", backend=_echo_backend)
    translator.enable_persistence(path)
    assert translator.try_cached(["Hello"]) == ["Merhaba"]
    assert translator.pending_upgrade(["Hello"]) == []


def test_old_format_cache_still_loads(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"tr": {"Hello": "Merhaba"}}, ensure_ascii=False), encoding="utf-8")
    translator = Translator("tr", backend=_echo_backend)
    translator.enable_persistence(path)
    assert translator.try_cached(["Hello"]) == ["Merhaba"]
    assert translator.pending_upgrade(["Hello"]) == []


def test_eviction_cleans_local_tier_bookkeeping():
    local = _StubLocal()
    translator = Translator("tr", backend=_echo_backend, cache_size=2, local=local)
    translator.prefill_local(["Oldest local line"])
    translator.translate_lines(["Second line", "Third line"])  # evicts the oldest
    assert translator.try_cached(["Oldest local line"]) == [None]
    assert translator.pending_upgrade(["Oldest local line"]) == []
    assert translator._local_keys == set()


def test_background_two_phase_paints_local_then_cloud():
    local = _StubLocal()
    translator = Translator("tr", backend=_echo_backend, local=local)
    states: list[list[str | None]] = []
    first, second = threading.Event(), threading.Event()

    def on_ready() -> None:
        states.append(translator.try_cached(["Fresh sentence"]))
        (first if len(states) == 1 else second).set()

    background = BackgroundTranslator(translator, on_ready=on_ready, on_error=lambda _m: None)
    background.start()
    background.request(["Fresh sentence"])
    assert first.wait(3.0), "phase-1 (local paint) on_ready missing"
    assert second.wait(3.0), "phase-2 (cloud) on_ready missing"
    assert states[0] == ["L:Fresh sentence"]  # first paint came from the local tier
    assert translator.try_cached(["Fresh sentence"]) == ["XFresh sentence"]
    assert translator.pending_upgrade(["Fresh sentence"]) == []
    background.stop()
    background.join(2.0)
    assert not background.is_alive()


def test_slow_cloud_fetch_does_not_delay_next_local_paint():
    # The live bug: one worker did local prefill AND the cloud fetch, so a
    # slow fallback fetch (seconds while gtx is rate-limited) held the NEXT
    # subtitle's local paint hostage. Phase 2 now runs on its own thread.
    def slow_backend(text: str) -> str:
        time.sleep(0.8)
        return "\n".join(f"X{part}" for part in text.split("\n"))

    local = _StubLocal()
    translator = Translator("tr", backend=slow_backend, local=local)
    ready = threading.Event()
    background = BackgroundTranslator(translator, on_ready=ready.set, on_error=lambda _m: None)
    background.start()
    background.request(["First subtitle"])
    assert ready.wait(2.0), "phase-1 paint of the first batch missing"
    ready.clear()
    time.sleep(0.1)  # the cloud thread is now inside the slow fetch
    started = time.monotonic()
    background.request(["Second subtitle"])
    assert ready.wait(2.0), "phase-1 paint of the second batch missing"
    assert time.monotonic() - started < 0.5, "local prefill waited behind the cloud fetch"
    assert translator.try_cached(["Second subtitle"]) == ["L:Second subtitle"]
    background.stop()
    background.join(2.0)
    assert not background.is_alive()


def test_upgrade_backlog_drains_only_when_gtx_available(monkeypatch):
    monkeypatch.setattr(translator_module, "_gtx_block_until", time.monotonic() + 60.0)
    backend_calls: list[str] = []

    def backend(text: str) -> str:
        backend_calls.append(text)
        return "\n".join(f"TR:{part}" for part in text.split("\n"))

    local = _StubLocal()
    translator = Translator("tr", backend=backend, local=local)
    translator.prefill_local(["Upgrade me now"])
    upgraded = threading.Event()
    background = BackgroundTranslator(translator, on_ready=upgraded.set, on_error=lambda _m: None)
    background.start()
    background.request_upgrade(translator.pending_upgrade(["Upgrade me now"]))
    time.sleep(0.35)
    assert backend_calls == []  # cooldown active: the backlog must wait
    translator_module._gtx_block_until = 0.0  # cooldown over
    assert upgraded.wait(3.0), "backlog did not drain once gtx became available"
    assert translator.try_cached(["Upgrade me now"]) == ["TR:Upgrade me now"]
    assert translator.pending_upgrade(["Upgrade me now"]) == []
    background.stop()
    background.join(2.0)


def test_request_upgrade_bounds_backlog_and_attempts():
    translator = Translator("tr", backend=_echo_backend)
    background = BackgroundTranslator(translator, on_ready=lambda: None, on_error=lambda _m: None)
    # never started: state is inspected directly
    background._upgrade_attempts["tired text"] = 3
    background.request_upgrade(["tired text"])
    assert background._upgrade == []  # attempt cap reached: dropped
    background._upgrade = [f"text {i}" for i in range(200)]
    background._upgrade_queued = {f"text {i}" for i in range(200)}
    background.request_upgrade(["one more"])
    assert len(background._upgrade) == 200  # backlog cap: dropped
    background.request_upgrade(["text 5"])
    assert background._upgrade.count("text 5") == 1  # already queued: no duplicate
