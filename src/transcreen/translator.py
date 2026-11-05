"""Translation: fast gtx endpoint with session reuse, caching, batching.

Primary backend is Google's translate_a/single (gtx) JSON endpoint over a
keep-alive session (~100-300 ms measured). While gtx is rate-limited
(429/403 or a google.com/sorry redirect) an ordered fallback chain takes
over: MyMemory (genuine translations, ~700 ms measured, hard 500-char
limit) and then the deep-translator Google web endpoint (~300-400 ms but
echo-prone under rate limiting). The cache persists to disk so daily-use
text translates instantly across restarts.

An optional offline tier (local_mt.LocalTranslator, duck-typed here as
LocalMt) prefills provisional "local" translations so new text paints in
Turkish within a loop cycle; a cloud result later overwrites and thereby
silently upgrades them, and BackgroundTranslator re-requests on-screen
local-tier text whenever gtx is reachable again.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_GTX_URL = "https://translate.googleapis.com/translate_a/single"
_AUTOSAVE_ENTRIES = 25
_AUTOSAVE_SECONDS = 60.0
_GTX_BLOCK_SECONDS = 90.0  # skip gtx after a 429 so we stop hammering it
_ECHO_ACCEPT_STRIKES = 3  # untrusted echoes needed before "untranslatable" is accepted
_ECHO_STRIKES_MAX = 800  # bookkeeping bound; cleared wholesale beyond this

_TURKISH_CHARS = set("çğıİöşüÇĞÖŞÜ")

# Top-level cache-file section holding provisional local-tier entries. Old
# versions skip it harmlessly: its values are dicts, not strings.
_LOCAL_SECTION = "__local__"

_tls_http = threading.local()
_gtx_block_until = 0.0


def _is_echo(source: str, translated: str) -> bool:
    return " ".join(source.split()).casefold() == " ".join(translated.split()).casefold()


def _http_session():
    session = getattr(_tls_http, "session", None)
    if session is None:
        import requests

        session = _tls_http.session = requests.Session()
    return session


def gtx_join_segments(payload: object) -> str:
    """Join the translated segments of a translate_a/single response."""
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        raise ValueError("unexpected gtx payload shape")
    parts: list[str] = []
    for segment in payload[0]:
        if isinstance(segment, list) and segment and isinstance(segment[0], str):
            parts.append(segment[0])
    return "".join(parts)


class _GtxSorryRedirectError(RuntimeError):
    """gtx answered via Google's rate-limit interstitial (google.com/sorry)."""


def _is_gtx_rate_limited(exc: Exception) -> bool:
    """True when the failure means "back off", not just "this call broke".

    Google signals rate limiting as HTTP 429, HTTP 403, or a redirect to the
    google.com/sorry page (observed: the sorry page itself sometimes
    comes back as a 200 captcha, which raise_for_status would miss).
    """
    if isinstance(exc, _GtxSorryRedirectError):
        return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) in (429, 403):
        return True
    return "google.com/sorry" in str(getattr(response, "url", "") or "")


def _gtx_translate(text: str, target: str) -> str:
    response = _http_session().post(
        _GTX_URL,
        params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t"},
        data={"q": text},
        timeout=8,
    )
    if "google.com/sorry" in str(response.url):
        raise _GtxSorryRedirectError("gtx redirected to the google.com/sorry rate-limit page")
    response.raise_for_status()
    return gtx_join_segments(response.json())


def gtx_available() -> bool:
    """True while the primary gtx endpoint is not in a rate-limit cooldown."""
    return time.monotonic() >= _gtx_block_until


_MYMEMORY_MAX_CHARS = 500  # the MyMemory API hard-rejects longer payloads

# MyMemory reports problems (quota exhausted, bad langpair) as 200-OK ALL-CAPS
# banner text in the translation field; caching one would poison the cache.
_MYMEMORY_ERROR_MARKERS = ("MYMEMORY", "INVALID SOURCE LANGUAGE", "QUERY LENGTH LIMIT")


def _mymemory_language(target: str) -> str | None:
    """Map a short code like "tr" to MyMemory's region-coded "tr-TR"."""
    from deep_translator.constants import MY_MEMORY_LANGUAGES_TO_CODES

    wanted = target.lower()
    prefix = wanted + "-"
    for code in MY_MEMORY_LANGUAGES_TO_CODES.values():
        lowered = code.lower()
        if lowered == wanted or lowered.startswith(prefix):
            return code
    return None


def _mymemory_translate(text: str, target: str) -> str:
    """Translate via MyMemory; raises when the payload or target does not fit.

    Observed (2026-07): the API rejects source "auto" with an
    error banner, but "Autodetect" — settable only through the source property,
    because deep-translator's own language list does not know it — returns
    genuine translations and preserves newline structure in batches.
    """
    if len(text) > _MYMEMORY_MAX_CHARS:
        raise ValueError(f"payload over MyMemory's {_MYMEMORY_MAX_CHARS}-char limit")
    code = _mymemory_language(target)
    if code is None:
        raise ValueError(f"no MyMemory language code for {target!r}")
    from deep_translator import MyMemoryTranslator

    translator = MyMemoryTranslator(source="auto", target=code)
    translator.source = "Autodetect"
    result = translator.translate(text)
    if not isinstance(result, str) or not result.strip():
        raise ValueError("MyMemory returned no usable translation")
    upper = result.upper()
    if any(marker in upper for marker in _MYMEMORY_ERROR_MARKERS):
        raise ValueError(f"MyMemory returned an error banner: {result[:60]!r}")
    return result


MAX_CHUNK = 4000  # Google web endpoint rejects ~5000+, stay comfortably below

# Tokens end at a sentence terminator or newline; together they partition the
# text exactly, so chunks can be re-joined losslessly.
_TOKEN_RE = re.compile(r"[^.!?…\n]*[.!?…\n]?")

# At least one letter (any script); digits/punctuation-only lines are skipped.
_LETTER_RE = re.compile(r"[^\W\d_]")

TranslateFn = Callable[[str], str]


_VOWELS = set("aeıioöuüâîûAEIİOÖUÜ")


def is_translatable(text: str) -> bool:
    """True when the text contains letters worth sending to a translator."""
    return bool(_LETTER_RE.search(text))


def looks_like_text(text: str) -> bool:
    """Filter out OCR junk: icon scribbles, separators, garbled fragments.

    Requires at least two letters, a majority of letters among visible
    characters, and (for longer runs) at least one vowel — consonant-only
    strings of 4+ letters are practically always misreads, not words.
    """
    stripped = text.strip()
    letters = _LETTER_RE.findall(stripped)
    if len(letters) < 2:
        return False
    visible = [c for c in stripped if not c.isspace()]
    if len(letters) / len(visible) < 0.5:
        return False
    return not (len(letters) >= 4 and not any(c in _VOWELS for c in stripped))


class TranslationError(RuntimeError):
    """Raised when the translation backend fails (usually connectivity)."""


def _pack_lines(lines: list[str], limit: int) -> list[list[str]]:
    """Group lines so each newline-joined group stays under the request limit."""
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            groups.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        groups.append(current)
    return groups


def split_sentences(text: str) -> list[str]:
    """Split text into sentence-ish tokens ending at a terminator or newline.

    The tokens partition the text exactly (joining them restores it); the
    local MT tier feeds them to the model one by one.
    """
    return [token for token in _TOKEN_RE.findall(text) if token]


def chunk_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split text into <=limit chunks, preferring sentence/newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for token in split_sentences(text):
        while len(token) > limit:  # monster sentence: hard cut
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(token[:limit])
            token = token[limit:]
        if buf and len(buf) + len(token) > limit:
            chunks.append(buf)
            buf = ""
        buf += token
    if buf:
        chunks.append(buf)
    return chunks


class LocalMt(Protocol):
    """Duck-typed offline translation backend (see local_mt.LocalTranslator)."""

    def available(self) -> bool: ...

    def translate_lines(self, lines: list[str]) -> list[str]: ...


class Translator:
    def __init__(
        self,
        target: str = "tr",
        backend: TranslateFn | None = None,
        cache_size: int = 5000,
        batch_limit: int = MAX_CHUNK,
        local: LocalMt | None = None,
    ) -> None:
        self.target = target
        self._backend = backend
        self._batch_limit = batch_limit
        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()  # cache is shared across worker threads
        self._local = local
        # Keys whose cached value came from the local model: provisional,
        # replaced (promoted) by the first cloud translation that arrives.
        self._local_keys: set[tuple[str, str]] = set()
        self._echo_strikes: dict[tuple[str, str], int] = {}
        self._echo_accepted: set[tuple[str, str]] = set()
        self._persist_path: Path | None = None
        self._added_since_save = 0
        self._last_save = time.monotonic()

    def set_target(self, target: str) -> None:
        self.target = target  # cache keys include the target, no need to clear

    def _call_backend(self, text: str) -> tuple[str, bool]:
        """Translate one payload; the bool says whether the result is TRUSTED.

        An injected backend short-circuits everything (tests). Otherwise the
        candidates run in order — gtx first, then the untrusted fallback
        chain — each with its own failure handling. Fallback results are
        untrusted: while Google rate-limits us a fallback sometimes echoes
        the source back, and caching that echo would freeze the text as
        "already Turkish" forever.
        """
        global _gtx_block_until
        if self._backend is not None:
            return self._backend(text), True
        if gtx_available():
            try:
                return _gtx_translate(text, self.target), True
            except Exception as exc:
                if _is_gtx_rate_limited(exc):
                    _gtx_block_until = time.monotonic() + _GTX_BLOCK_SECONDS
                    logger.info(
                        "gtx rate-limited (%s), using the fallback chain for %.0f s",
                        type(exc).__name__,
                        _GTX_BLOCK_SECONDS,
                    )
                else:
                    logger.debug("gtx failed, trying the fallback chain", exc_info=True)
        last_error: Exception | None = None
        for name, candidate in self._fallback_chain(text):
            try:
                translated = candidate(text)
            except Exception as exc:
                last_error = exc
                logger.debug("fallback %s failed", name, exc_info=True)
                continue
            if translated and translated.strip():
                return translated, False
            logger.debug("fallback %s returned an empty result", name)
        if last_error is not None:
            raise last_error
        return "", False

    def _fallback_chain(self, text: str) -> list[tuple[str, TranslateFn]]:
        """Ordered untrusted fallbacks for while gtx is unavailable.

        MyMemory only fits payloads within its 500-char limit; longer batches
        go straight to the Google web fallback.
        """
        chain: list[tuple[str, TranslateFn]] = []
        if len(text) <= _MYMEMORY_MAX_CHARS:
            chain.append(("MyMemory", self._mymemory_fallback))
        chain.append(("GoogleWeb", self._google_fallback))
        return chain

    def _mymemory_fallback(self, text: str) -> str:
        return _mymemory_translate(text, self.target)

    def _google_fallback(self, text: str) -> str:
        from deep_translator import GoogleTranslator

        return GoogleTranslator(source="auto", target=self.target).translate(text)

    def _cache_key(self, text: str) -> tuple[str, str]:
        # Whitespace-insensitive so live-mode OCR jitter still hits the cache.
        return (self.target, " ".join(text.split()))

    def _cache_entry(self, key: tuple[str, str]) -> tuple[str | None, bool]:
        """The cached value plus whether it is a provisional local-tier entry."""
        with self._lock:
            cached = self._cache.get(key)
            # An empty stored value would freeze the line as "translated as
            # nothing" forever; report it as a miss so it gets re-requested.
            if cached is None or not cached.strip():
                return None, False
            self._cache.move_to_end(key)
            return cached, key in self._local_keys

    def _cache_get(self, key: tuple[str, str]) -> str | None:
        return self._cache_entry(key)[0]

    def _cache_put(self, key: tuple[str, str], value: str, local: bool = False) -> None:
        if not value.strip():
            logger.debug("not caching empty translation for %r", key[1])
            return
        with self._lock:
            self._cache[key] = value
            if local:
                self._local_keys.add(key)
            else:
                self._local_keys.discard(key)  # a cloud store promotes the entry
            self._added_since_save += 1
            while len(self._cache) > self._cache_size:
                evicted, _ = self._cache.popitem(last=False)
                self._local_keys.discard(evicted)

    # -------------------------------------------------------- persistence
    def enable_persistence(self, path: Path) -> None:
        """Load the disk cache and auto-save it as new entries accumulate."""
        self._persist_path = path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        # Local-tier sections load FIRST so a cloud entry for the same key in
        # a normal section wins the collision and clears the local tag.
        sections: list[tuple[Any, Any, bool]] = []
        local_raw = raw.get(_LOCAL_SECTION)
        if isinstance(local_raw, dict):
            sections += [(target, entries, True) for target, entries in local_raw.items()]
        sections += [(t, entries, False) for t, entries in raw.items() if t != _LOCAL_SECTION]
        dropped = 0
        with self._lock:
            for target, entries, local in sections:
                if not isinstance(entries, dict):
                    continue
                for text, translated in entries.items():
                    if not (isinstance(text, str) and isinstance(translated, str)):
                        continue
                    if not translated.strip():
                        dropped += 1  # poisoned entry written by an older version
                        continue
                    if _is_echo(text, translated) and not (set(text) & _TURKISH_CHARS):
                        # A non-Turkish source "translated" to itself is almost
                        # certainly a rate-limited fallback echo, not Turkish.
                        dropped += 1
                        continue
                    key = (target, text)
                    self._cache[key] = translated
                    if local:
                        self._local_keys.add(key)
                    else:
                        self._local_keys.discard(key)
            while len(self._cache) > self._cache_size:
                evicted, _ = self._cache.popitem(last=False)
                self._local_keys.discard(evicted)
            self._added_since_save = 0
        if dropped:
            logger.debug("dropped %d empty entries while loading the cache", dropped)
        logger.info("translation cache loaded: %d entries", len(self._cache))

    def save_cache(self) -> None:
        if self._persist_path is None:
            return
        with self._lock:
            snapshot = [
                (key, value, key in self._local_keys)
                for key, value in self._cache.items()
                if key not in self._echo_accepted  # session-only, never persisted
            ]
            self._added_since_save = 0
        self._last_save = time.monotonic()
        cloud: dict[str, dict[str, str]] = {}
        local_tier: dict[str, dict[str, str]] = {}
        for (target, text), translated, is_local in snapshot:
            bucket = local_tier if is_local else cloud
            bucket.setdefault(target, {})[text] = translated
        data: dict[str, object] = dict(cloud)
        if local_tier:
            data[_LOCAL_SECTION] = local_tier
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("could not save translation cache", exc_info=True)

    def _maybe_autosave(self) -> None:
        if self._persist_path is None or self._added_since_save == 0:
            return
        overdue = time.monotonic() - self._last_save > _AUTOSAVE_SECONDS
        if self._added_since_save >= _AUTOSAVE_ENTRIES or overdue:
            self.save_cache()

    def try_cached(self, lines: list[str]) -> list[str | None]:
        """Cache-only lookup: never touches the network."""
        return [self._cache_get(self._cache_key(line)) if line.strip() else "" for line in lines]

    def prefill_local(self, lines: list[str], *, autosave: bool = True) -> bool:
        """Provisionally fill cache misses from the local model (local tier).

        Only lines with no cache entry at all are considered; lines carrying
        Turkish-specific characters are skipped (the model is en->tr only).
        Returns True when at least one entry was stored. Silent no-op while
        the local model is absent or still loading. ``autosave=False`` skips
        the disk flush — the live loop calls this inline and must not stall
        on a JSON write; the background thread flushes soon after.
        """
        if self._local is None or not self._local.available():
            return False
        candidates: list[tuple[str, tuple[str, str]]] = []
        seen: set[tuple[str, str]] = set()
        for line in lines:
            sanitized = line.replace("\n", " ").strip()
            if not sanitized or set(sanitized) & _TURKISH_CHARS:
                continue
            key = self._cache_key(sanitized)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((sanitized, key))
        if not candidates:
            return False
        with self._lock:
            todo = [text for text, key in candidates if key not in self._cache]
        if not todo:
            return False
        results = self._local.translate_lines(todo)
        stored = False
        for source, translated in zip(todo, results, strict=True):
            cleaned = translated.strip()
            # A local echo would poison the cache exactly like a fallback
            # echo ("already Turkish" freeze); never store one.
            if not cleaned or _is_echo(source, cleaned):
                continue
            self._cache_put(self._cache_key(source), cleaned, local=True)
            stored = True
        if stored and autosave:
            self._maybe_autosave()
        return stored

    def pending_upgrade(self, lines: list[str]) -> list[str]:
        """The subset of lines currently served by the provisional local tier."""
        out: list[str] = []
        seen: set[tuple[str, str]] = set()
        with self._lock:
            for line in lines:
                sanitized = line.replace("\n", " ").strip()
                if not sanitized:
                    continue
                key = self._cache_key(sanitized)
                if key in seen or key not in self._local_keys or key not in self._cache:
                    continue
                seen.add(key)
                out.append(sanitized)
        return out

    def translate_lines(self, lines: list[str]) -> list[str]:
        """Translate many short lines with one batched request.

        Lines are deduplicated and joined with newlines (Google preserves them);
        if the service returns a different line count, or empty parts within a
        matching count, the affected lines are retried individually (and
        concurrently). A line whose retry still comes back empty stays uncached
        and yields "" for this call only, so a later request tries it again.
        Results come from / go into the shared cache. A provisional local-tier
        hit counts as a miss here: the cloud result overwrites (upgrades) it,
        while the local text keeps serving reads until that happens.
        """
        sanitized = [line.replace("\n", " ").strip() for line in lines]
        missing: list[str] = []
        seen: set[tuple[str, str]] = set()
        for line in sanitized:
            key = self._cache_key(line)
            if not line or key in seen:
                continue
            cached, local = self._cache_entry(key)
            if cached is not None and not local:
                continue
            seen.add(key)
            missing.append(line)

        groups = _pack_lines(missing, self._batch_limit)
        if len(groups) <= 1:
            for group in groups:
                self._process_group(group)
        else:
            # A full page of new text spans several requests; fetching them
            # concurrently keeps the first paint at roughly one round-trip.
            with ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
                list(pool.map(self._process_group, groups))

        self._maybe_autosave()
        return [
            (self._cache_get(self._cache_key(line)) or "") if line else "" for line in sanitized
        ]

    def _process_group(self, group: list[str]) -> None:
        """Fetch one newline-joined group and cache its healthy parts."""
        joined = "\n".join(group)
        translated_joined, trusted = self._call_backend_safe(joined)
        parts = translated_joined.split("\n")
        if len(parts) != len(group):
            logger.info(
                "batch line count mismatch (%d -> %d), retrying individually",
                len(group),
                len(parts),
            )
            self._translate_individually(group)
            return
        retry: list[str] = []
        for source, translated in zip(group, parts, strict=True):
            if self._store_result(source, translated, trusted) is None:
                retry.append(source)
        if retry:
            logger.info("batch returned %d unusable part(s), retrying individually", len(retry))
            self._translate_individually(retry)

    def _translate_individually(self, sources: list[str]) -> None:
        """Translate lines one by one, concurrently; unusable results stay uncached.

        _http_session is thread-local, so parallel backend calls are safe.
        pool.map preserves input order and re-raises worker TranslationErrors.
        """
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self._call_backend_safe, sources))
        for source, (translated, trusted) in zip(sources, results, strict=True):
            if self._store_result(source, translated, trusted) is None:
                logger.debug("individual retry unusable for %r, leaving uncached", source)

    def _store_result(self, source: str, translated: str, trusted: bool) -> str | None:
        """Cache a translation unless it is empty or an untrusted source echo.

        An untrusted echo usually means the rate-limited fallback bounced the
        input back, so it is retried — but a text that echoes on several
        separate attempts is really untranslatable (proper nouns like game
        titles), and accepting it stops an endless retry loop while gtx is
        blocked. Any trusted result clears the strike count.
        """
        cleaned = translated.strip()
        if not cleaned:
            return None
        key = self._cache_key(source)
        if not trusted and _is_echo(source, cleaned):
            with self._lock:
                strikes = self._echo_strikes.get(key, 0) + 1
                accepted = strikes >= _ECHO_ACCEPT_STRIKES
                if accepted:
                    self._echo_strikes.pop(key, None)
                else:
                    if len(self._echo_strikes) > _ECHO_STRIKES_MAX:
                        self._echo_strikes.clear()
                    self._echo_strikes[key] = strikes
            if not accepted:
                return None  # rate-limited fallback echoing the input; retry later
            existing, existing_local = self._cache_entry(key)
            if existing_local and existing is not None and existing != cleaned:
                # Accepting "untranslatable" must not clobber a differing
                # provisional local translation — the local text is strictly
                # more useful than the echoed source.
                logger.debug("keeping the local translation for %r over an accepted echo", source)
                return existing
            logger.debug("accepting %r as untranslatable after repeated echoes", source)
            # Session-only acceptance: a 429-era echo of a real sentence must
            # not be fossilized on disk, so it is retried on the next start.
            with self._lock:
                self._echo_accepted.add(key)
        elif trusted:
            with self._lock:
                self._echo_strikes.pop(key, None)
                self._echo_accepted.discard(key)
        self._cache_put(key, cleaned)
        return cleaned

    def _call_backend_safe(self, text: str) -> tuple[str, bool]:
        try:
            translated, trusted = self._call_backend(text)
            return (translated or "", trusted)
        except TranslationError:
            raise
        except Exception as exc:
            raise TranslationError(f"translation backend failed: {exc}") from exc

    def translate(self, text: str) -> str:
        if not text.strip():
            return ""
        key = self._cache_key(text)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        outputs = [self._call_backend_safe(chunk) for chunk in chunk_text(text) if chunk.strip()]
        result = " ".join(part.strip() for part, _trusted in outputs if part.strip())
        if all(trusted for _part, trusted in outputs) or not _is_echo(text, result):
            self._cache_put(key, result)
        return result


def pacing_delay(last_fetch: float, now: float, min_interval: float) -> float:
    """Seconds to wait so consecutive fetches stay at least min_interval apart.

    Pure helper (unit-testable without sleeping): 0.0 when the next fetch is
    already due, otherwise the remaining part of the interval. Seed last_fetch
    with -inf to let the first fetch go out immediately.
    """
    return max(0.0, last_fetch + min_interval - now)


class BackgroundTranslator(threading.Thread):
    """Fetches missing translations off the OCR thread, in two phases.

    The live loop renders cached translations immediately and hands misses to
    this thread. Phase 1: the batch is prefilled from the local model and, if
    anything was stored, on_ready fires immediately (provisional paint before
    any network pacing). Phase 2: the same batch goes to the cloud path,
    whose results overwrite (upgrade) the local tier, then on_ready fires
    again. Consecutive cloud fetches stay MIN_FETCH_INTERVAL_S apart so a
    burst of screen changes cannot provoke Google's rate limiter; failed
    batches are requeued with a backoff so a dropped connection heals itself.

    request_upgrade feeds a bounded backlog of on-screen local-tier texts;
    it drains in small slices, only while nothing fresh is pending and gtx
    is out of its rate-limit cooldown.
    """

    RETRY_BACKOFF_S = 2.0
    MIN_FETCH_INTERVAL_S = 0.2
    UPGRADE_BATCH = 20
    UPGRADE_BACKLOG_MAX = 200
    UPGRADE_ATTEMPTS_MAX = 3
    _UPGRADE_ATTEMPTS_BOUND = 1000  # bookkeeping bound; cleared wholesale beyond

    def __init__(
        self,
        translator: Translator,
        on_ready: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__(name="translate", daemon=True)
        self._translator = translator
        self._on_ready = on_ready
        self._on_error = on_error
        self._pending: list[str] = []
        self._queued: set[str] = set()
        self._upgrade: list[str] = []
        self._upgrade_queued: set[str] = set()
        self._upgrade_attempts: dict[str, int] = {}
        self._cond = threading.Condition()
        self._stopped = False
        # Phase 2 runs on its own thread: a slow cloud fetch (seconds, while
        # gtx is rate-limited and the fallbacks retry line by line) must never
        # delay the NEXT batch's local paint — measured live, this alone made
        # video subtitles turn Turkish a second or two late.
        self._cloud_jobs: list[tuple[list[str], bool]] = []  # (texts, is_fresh_batch)
        self._cloud_cond = threading.Condition()
        self._cloud = threading.Thread(target=self._cloud_loop, name="translate-cloud", daemon=True)

    def start(self) -> None:
        super().start()
        self._cloud.start()

    def request(self, texts: list[str]) -> None:
        with self._cond:
            added = False
            for text in texts:
                normalized = " ".join(text.split())
                if normalized and normalized not in self._queued:
                    self._queued.add(normalized)
                    self._pending.append(text)
                    added = True
            if added:
                self._cond.notify()

    def request_upgrade(self, texts: list[str]) -> None:
        """Queue local-tier texts for a silent cloud quality upgrade.

        Bounded on purpose: at most UPGRADE_BACKLOG_MAX texts wait and each
        text is attempted at most UPGRADE_ATTEMPTS_MAX times, so a
        permanently rate-limited gtx cannot grow an endless retry queue.
        """
        with self._cond:
            added = False
            for text in texts:
                normalized = " ".join(text.split())
                if (
                    not normalized
                    or normalized in self._queued
                    or normalized in self._upgrade_queued
                ):
                    continue
                if self._upgrade_attempts.get(normalized, 0) >= self.UPGRADE_ATTEMPTS_MAX:
                    continue
                if len(self._upgrade) >= self.UPGRADE_BACKLOG_MAX:
                    break
                self._upgrade.append(text)
                self._upgrade_queued.add(normalized)
                added = True
            if added:
                self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify()
        with self._cloud_cond:
            self._cloud_cond.notify()

    def _take_upgrade_batch_locked(self) -> list[str]:
        """Pop one drain slice off the backlog; caller holds self._cond."""
        batch = self._upgrade[: self.UPGRADE_BATCH]
        del self._upgrade[: self.UPGRADE_BATCH]
        if len(self._upgrade_attempts) > self._UPGRADE_ATTEMPTS_BOUND:
            self._upgrade_attempts.clear()
        for text in batch:
            normalized = " ".join(text.split())
            self._upgrade_queued.discard(normalized)
            self._upgrade_attempts[normalized] = self._upgrade_attempts.get(normalized, 0) + 1
        return batch

    def run(self) -> None:
        """Phase 1 loop: local prefill, then hand the texts to the cloud thread."""
        while True:
            upgrades: list[str] = []
            with self._cond:
                while not self._pending and not self._stopped:
                    if self._upgrade and gtx_available() and not self._cloud_busy():
                        break
                    # While upgrades wait out the gtx cooldown, poll: nothing
                    # notifies the cooldown's end.
                    self._cond.wait(timeout=1.0 if self._upgrade else None)
                if self._stopped:
                    return
                if self._pending:
                    batch = self._pending
                    self._pending = []
                else:
                    batch = []
                    upgrades = self._take_upgrade_batch_locked()

            if batch:
                # Phase 1: provisional paint from the local model, before any
                # network pacing — new text turns Turkish within ~one cycle.
                try:
                    if self._translator.prefill_local(batch):
                        self._on_ready()
                except Exception:
                    logger.exception("local prefill failed")
            self._submit_cloud(batch or upgrades, is_batch=bool(batch))

    def _cloud_busy(self) -> bool:
        with self._cloud_cond:
            return bool(self._cloud_jobs)

    def _submit_cloud(self, texts: list[str], *, is_batch: bool, front: bool = False) -> None:
        if not texts:
            return
        with self._cloud_cond:
            if front:
                self._cloud_jobs.insert(0, (texts, is_batch))
            else:
                self._cloud_jobs.append((texts, is_batch))
            self._cloud_cond.notify()

    def _cloud_loop(self) -> None:
        """Phase 2 loop: paced cloud fetches that upgrade (or fill) the cache."""
        last_fetch = float("-inf")  # the first fetch goes out immediately
        while True:
            with self._cloud_cond:
                while not self._cloud_jobs:
                    if self._stopped:
                        return
                    self._cloud_cond.wait(timeout=1.0)
                texts, is_batch = self._cloud_jobs.pop(0)
            # One bounded sleep outside the lock, no busy loop: jobs can keep
            # queueing while we wait out the remaining interval.
            delay = pacing_delay(last_fetch, time.monotonic(), self.MIN_FETCH_INTERVAL_S)
            if delay > 0.0:
                time.sleep(delay)
            last_fetch = time.monotonic()
            try:
                self._translator.translate_lines(texts)
            except TranslationError as exc:
                logger.warning("background translation failed: %s", exc)
                if is_batch:
                    self._on_error(str(exc))
                    time.sleep(self.RETRY_BACKOFF_S)
                    self._submit_cloud(texts, is_batch=True, front=True)
                else:
                    # Upgrades stay best-effort: the screen already shows the
                    # local text and the app re-requests it while it remains
                    # local-tier, so no toast and no requeue — just back off.
                    time.sleep(self.RETRY_BACKOFF_S)
                continue
            if is_batch:
                with self._cond:
                    for text in texts:
                        self._queued.discard(" ".join(text.split()))
            self._on_ready()
