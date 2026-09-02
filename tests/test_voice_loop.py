"""
test_voice_loop.py -- tests for the voice adapters and their API wiring.

Everything network/model-facing is mocked: STT tests inject a fake
faster_whisper module, TTS tests inject a fake edge_tts module, and the API
tests monkeypatch services.stt/services.tts functions directly. No test here
downloads a Whisper model or touches Microsoft's speech servers -- the real
end-to-end audio round trip is demonstrated separately (see README).
"""
import asyncio
import sys
import types

import pytest
from fastapi.testclient import TestClient

import services.stt as stt
import services.tts as tts
from backend.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# STT adapter (faster-whisper mocked)
# ---------------------------------------------------------------------------
class _FakeSegment:
    def __init__(self, text):
        self.text = text


class _FakeInfo:
    language = "en"
    duration = 1.25


class _FakeWhisperModel:
    instances = 0
    fail = False

    def __init__(self, *args, **kwargs):
        _FakeWhisperModel.instances += 1

    def transcribe(self, path, **kwargs):
        if _FakeWhisperModel.fail:
            raise RuntimeError("decoder exploded")
        return iter([_FakeSegment("  mini truck "), _FakeSegment("under 5 lakh ")]), _FakeInfo()


@pytest.fixture()
def fake_faster_whisper(monkeypatch):
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    monkeypatch.setattr(stt, "_model", None)
    monkeypatch.setattr(stt, "_import_checked", False)
    _FakeWhisperModel.instances = 0
    _FakeWhisperModel.fail = False
    return mod


def test_stt_transcribe_joins_segments(fake_faster_whisper):
    out = stt.transcribe(b"RIFF-fake-wav-bytes", language="en")
    assert out["text"] == "mini truck under 5 lakh"
    assert out["language"] == "en"
    assert out["duration"] == 1.25
    assert out["model"] == stt.MODEL_NAME
    assert stt.is_available() is True


def test_stt_model_is_lazy_singleton(fake_faster_whisper):
    stt.transcribe(b"RIFF-fake-wav-bytes")
    stt.transcribe(b"RIFF-fake-wav-bytes")
    assert _FakeWhisperModel.instances == 1  # loaded once, reused


def test_stt_rejects_empty_and_oversized(fake_faster_whisper, monkeypatch):
    with pytest.raises(stt.STTError):
        stt.transcribe(b"")
    monkeypatch.setattr(stt, "MAX_AUDIO_BYTES", 8)
    with pytest.raises(stt.STTError):
        stt.transcribe(b"x" * 9)


def test_stt_wraps_model_failure(fake_faster_whisper):
    _FakeWhisperModel.fail = True
    with pytest.raises(stt.STTError):
        stt.transcribe(b"RIFF-fake-wav-bytes")


def test_stt_is_available_false_when_unimportable(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import -> ImportError
    monkeypatch.setattr(stt, "_import_checked", False)
    monkeypatch.setattr(stt, "_importable", False)  # restore after test (is_available mutates it)
    assert stt.is_available() is False


# ---------------------------------------------------------------------------
# TTS adapter (edge-tts mocked)
# ---------------------------------------------------------------------------
class _FakeCommunicate:
    calls = 0
    fail_with = None
    slow = False

    def __init__(self, text, voice):
        _FakeCommunicate.calls += 1
        self.text, self.voice = text, voice

    async def stream(self):
        if _FakeCommunicate.fail_with:
            raise _FakeCommunicate.fail_with
        if _FakeCommunicate.slow:
            await asyncio.sleep(5)
        yield {"type": "audio", "data": b"ID3-fake"}
        yield {"type": "WordBoundary", "data": b"ignored"}
        yield {"type": "audio", "data": b"-mp3"}


@pytest.fixture()
def fake_edge_tts(monkeypatch):
    mod = types.ModuleType("edge_tts")
    mod.Communicate = _FakeCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", mod)
    monkeypatch.setattr(tts, "_cache", tts.OrderedDict())
    _FakeCommunicate.calls = 0
    _FakeCommunicate.fail_with = None
    _FakeCommunicate.slow = False
    return mod


def test_tts_synthesize_returns_mp3_bytes(fake_edge_tts):
    audio = asyncio.run(tts.synthesize_async("hello"))
    assert audio == b"ID3-fake-mp3"
    assert tts.is_configured() is True


def test_tts_cache_skips_second_call(fake_edge_tts):
    asyncio.run(tts.synthesize_async("same text"))
    asyncio.run(tts.synthesize_async("same text"))
    assert _FakeCommunicate.calls == 1  # second call served from LRU cache
    asyncio.run(tts.synthesize_async("other text"))
    assert _FakeCommunicate.calls == 2


def test_tts_empty_text_raises(fake_edge_tts):
    with pytest.raises(tts.TTSError):
        asyncio.run(tts.synthesize_async("   "))


def test_tts_network_failure_raises_tts_error(fake_edge_tts):
    _FakeCommunicate.fail_with = ConnectionError("speech platform unreachable")
    with pytest.raises(tts.TTSError):
        asyncio.run(tts.synthesize_async("hello"))


def test_tts_timeout_raises_tts_error(fake_edge_tts, monkeypatch):
    _FakeCommunicate.slow = True
    monkeypatch.setattr(tts, "TTS_TIMEOUT", 0.05)
    with pytest.raises(tts.TTSError, match="timed out"):
        asyncio.run(tts.synthesize_async("hello"))


# ---------------------------------------------------------------------------
# API wiring: /api/voice multipart, /api/voice/audio, /api/tts
# ---------------------------------------------------------------------------
def _patch_stt(monkeypatch, text="mini truck under 5 lakh in Mumbai", error=None):
    def fake_transcribe(audio_bytes, language=None):
        if error:
            raise error
        return {"text": text, "language": "en", "duration": 2.0, "model": "tiny"}

    monkeypatch.setattr(stt, "transcribe", fake_transcribe)


def test_voice_audio_multipart_on_api_voice(monkeypatch):
    _patch_stt(monkeypatch)
    r = client.post("/api/voice",
                    files={"audio": ("rec.wav", b"RIFF-fake", "audio/wav")},
                    data={"session_id": "voice-sess"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"] == "mini truck under 5 lakh in Mumbai"
    assert body["slots"]["budget"] == 500_000 and body["slots"]["city"] == "Mumbai"
    assert body["latency_ms"]["stt"] >= 0
    assert body["stt"]["model"] == "tiny"
    assert len(body["results"]) >= 1


def test_voice_audio_endpoint_same_payload(monkeypatch):
    _patch_stt(monkeypatch)
    r = client.post("/api/voice/audio",
                    files={"audio": ("rec.wav", b"RIFF-fake", "audio/wav")},
                    data={"session_id": "voice-audio-sess"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"].startswith("mini truck")
    assert "spoken" in body and "results" in body


def test_voice_json_path_unchanged_after_multipart_landing(monkeypatch):
    r = client.post("/api/voice",
                    json={"session_id": "json-sess", "transcript": "mini truck under 5 lakh in Mumbai"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slots"]["budget"] == 500_000
    assert "stt" not in body  # typed path carries no stt block


def test_voice_audio_stt_error_is_400(monkeypatch):
    _patch_stt(monkeypatch, error=stt.STTError("corrupt audio"))
    r = client.post("/api/voice/audio",
                    files={"audio": ("rec.wav", b"RIFF-fake", "audio/wav")},
                    data={"session_id": "s"})
    assert r.status_code == 400
    assert r.json()["detail"]["status"] == "stt_error"


def test_voice_audio_no_speech_is_400(monkeypatch):
    _patch_stt(monkeypatch, text="   ")
    r = client.post("/api/voice/audio",
                    files={"audio": ("rec.wav", b"RIFF-fake", "audio/wav")},
                    data={"session_id": "s"})
    assert r.status_code == 400
    assert r.json()["detail"]["status"] == "no_speech"


def test_tts_returns_mp3(monkeypatch):
    async def fake_synth(text, voice=None):
        return b"ID3-fake-mp3-bytes"

    monkeypatch.setattr(tts, "synthesize_async", fake_synth)
    r = client.post("/api/tts", json={"text": "Here are the top matching vehicles."})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == b"ID3-fake-mp3-bytes"


def test_tts_unavailable_contract(monkeypatch):
    async def fake_synth(text, voice=None):
        raise tts.TTSError("edge-tts timed out after 12s")

    monkeypatch.setattr(tts, "synthesize_async", fake_synth)
    r = client.post("/api/tts", json={"text": "spoken answer"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "tts_unavailable"
    assert body["spoken_text"] == "spoken answer"


def test_tts_empty_text_is_400():
    r = client.post("/api/tts", json={"text": "   "})
    assert r.status_code == 400


def test_health_reports_adapters():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert set(body["adapters"]) == {"stt", "tts"}
