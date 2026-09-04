"""
stt.py -- speech-to-text adapter (faster-whisper, local, keyless).

Machine budget: 3.9 GB RAM total, ~1 GB available for inference -> the default
model is faster-whisper "tiny" with int8 quantization on CPU. Larger models
OOM-kill on this box; do not raise the default.

Public interface (the contract the pipeline depends on):
    transcribe(audio_bytes, language=None) -> {"text", "language", "duration", "model"}
    STTError                               -- raised on unusable audio / model failure
    is_available()                         -- True when faster-whisper is importable

Design notes
------------
* The model is lazy-loaded on first use and kept as a module singleton, so
  importing this module is cheap and the API stays responsive before the first
  voice request.
* One inference at a time (thread lock): concurrent transcriptions would spike
  RAM on this small box for zero latency benefit.
* No VAD filter: the silero VAD would trigger an extra model download. Input is
  expected to be short push-to-talk clips (WAV from the frontend).
* All configuration via env vars so operators can swap models without code
  changes: STT_MODEL / STT_DEVICE / STT_COMPUTE_TYPE / STT_LANGUAGE.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from typing import Optional

_LOGGER = logging.getLogger("vehiclevoice.stt")

MODEL_NAME = os.environ.get("STT_MODEL", "small")
DEVICE = os.environ.get("STT_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("STT_COMPUTE_TYPE", "int8")
LANGUAGE = os.environ.get("STT_LANGUAGE", "en")
MAX_AUDIO_BYTES = 10 * 1024 * 1024  # 10 MB hard cap on uploads

_model = None
_model_lock = threading.Lock()
_infer_lock = threading.Lock()
_import_checked = False
_importable = False


class STTError(RuntimeError):
    """Raised when audio cannot be transcribed."""


def is_available() -> bool:
    """True when the faster-whisper package is importable (no model load)."""
    global _import_checked, _importable
    if not _import_checked:
        try:
            import faster_whisper  # noqa: F401

            _importable = True
        except Exception:
            _importable = False
        _import_checked = True
    return _importable


def _get_model():
    """Lazy-load the WhisperModel singleton (downloads on first call)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from faster_whisper import WhisperModel
                except Exception as exc:  # pragma: no cover - import guard
                    raise STTError(f"faster-whisper not installed: {exc!r}") from exc
                _LOGGER.info(
                    "loading faster-whisper model %r (device=%s compute=%s)",
                    MODEL_NAME, DEVICE, COMPUTE_TYPE,
                )
                t0 = time.perf_counter()
                _model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
                _LOGGER.info("model loaded in %.1fs", time.perf_counter() - t0)
    return _model


def transcribe(audio_bytes: bytes, language: Optional[str] = None) -> dict:
    """Transcribe one audio clip (bytes: WAV/WEBM/OGG/MP3) to text.

    Returns a dict -- never a bare string -- so the caller can log/attach
    metadata without re-parsing. Raises STTError on unusable input.
    """
    if not audio_bytes:
        raise STTError("empty audio upload")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise STTError(f"audio too large ({len(audio_bytes)} bytes > {MAX_AUDIO_BYTES})")

    model = _get_model()
    lang = (language or LANGUAGE or None) or None

    # Write to a temp file: faster-whisper's decoder (PyAV) handles paths most
    # reliably across container formats.
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.close()
        # Serialize inference: one clip at a time on this RAM-constrained box.
        with _infer_lock:
            segments, info = model.transcribe(
                tmp.name,
                language=lang,
                beam_size=1,                       # greedy: fastest for tiny clips
                condition_on_previous_text=False,  # avoid tiny-model repetition loops
                vad_filter=False,                  # keep downloads minimal (no silero)
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
        return {
            "text": text,
            "language": getattr(info, "language", None),
            "duration": getattr(info, "duration", None),
            "model": MODEL_NAME,
        }
    except STTError:
        raise
    except Exception as exc:
        raise STTError(f"transcription failed: {exc!r}") from exc
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
