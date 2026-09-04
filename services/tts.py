"""
tts.py -- text-to-speech adapter (edge-tts: free Microsoft neural voices, no API key).

Public interface (the contract the pipeline depends on):
    await synthesize_async(text, voice=None) -> mp3 bytes
    synthesize(text, voice=None) -> mp3 bytes          (sync wrapper for scripts/tests)
    TTSError                                           -- raised on any failure
    VOICE / is_configured()                            -- introspection for /health

Honest fallback contract: this adapter depends on outbound access to Microsoft's
speech servers. If the network blocks it (or the service errors/times out), the
caller gets TTSError -- the backend turns that into an explicit
{"status": "tts_unavailable", "spoken_text": ...} JSON response. We never fake
audio; the spoken text is always returned so the UI can show it.

Design notes
------------
* Small in-memory LRU cache keyed by (voice, text): repeated identical sentences
  (common in demo/eval flows) skip the round trip entirely.
* Hard timeout (TTS_TIMEOUT, default 12s) so a dead network cannot hang a request.
* Voice default en-IN-NeerjaNeural (Indian English) to match the catalog domain.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections import OrderedDict
from typing import Optional

_LOGGER = logging.getLogger("vehiclevoice.tts")

VOICE = os.environ.get("TTS_VOICE", "en-IN-NeerjaNeural")
TTS_TIMEOUT = float(os.environ.get("TTS_TIMEOUT", "12"))
CACHE_MAX = int(os.environ.get("TTS_CACHE_MAX", "64"))
MAX_TEXT = 2000

_cache: "OrderedDict[tuple[str, str], bytes]" = OrderedDict()
_cache_lock = threading.Lock()


class TTSError(RuntimeError):
    """Raised when speech synthesis fails (network, timeout, empty input)."""


def is_configured() -> bool:
    """True when edge-tts is importable; network reachability is checked per call."""
    try:
        import edge_tts  # noqa: F401

        return True
    except Exception:
        return False


def _cache_get(key):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    return None


def _cache_put(key, audio: bytes) -> None:
    with _cache_lock:
        _cache[key] = audio
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


async def _stream_audio(text: str, voice: str) -> bytes:
    """Run one edge-tts synthesis. Raises on any failure."""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    chunks: list[bytes] = []

    async def _collect() -> None:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])

    try:
        await asyncio.wait_for(_collect(), timeout=TTS_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise TTSError(f"edge-tts timed out after {TTS_TIMEOUT}s") from exc
    except TTSError:
        raise
    except Exception as exc:
        raise TTSError(f"edge-tts failed: {exc!r}") from exc

    if not chunks:
        raise TTSError("edge-tts returned no audio")
    return b"".join(chunks)


async def synthesize_async(text: str, voice: Optional[str] = None) -> bytes:
    """Synthesize `text` to mp3 bytes. Raises TTSError on failure."""
    text = (text or "").strip()[:MAX_TEXT]
    if not text:
        raise TTSError("empty text")
    v = voice or VOICE
    key = (v, text)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    audio = await _stream_audio(text, v)
    _cache_put(key, audio)
    return audio


def synthesize(text: str, voice: Optional[str] = None) -> bytes:
    """Synchronous wrapper (for scripts and tests, not for async FastAPI routes)."""
    return asyncio.run(synthesize_async(text, voice))
