"""
main.py -- FastAPI app wiring the full VehicleVoice pipeline.

One app, clearly separated service modules, exactly the pipeline from the
business plan:

    (stt)  -> nlu.extract_slots -> conversation.merge -> search ->
              ranking.top_n(3) -> response.compose_response -> json -> (tts)

Endpoints
---------
POST /api/voice        {session_id, transcript} -> merged slots, top-3 records,
                       spoken answer, per-stage latency ms. The JSON-transcript
                       path (typed input) is unchanged.
POST /api/voice        multipart (session_id, audio file) -> same payload shape,
                       but the transcript comes from services/stt.py
                       (faster-whisper, local). stt stage latency is filled in
                       and a `stt` metadata block is attached.
POST /api/voice/audio  identical multipart handler (what the React UI posts to).
POST /api/tts          {text} -> mp3 bytes from services/tts.py (edge-tts). On
                       TTSError returns 200 JSON {"status": "tts_unavailable",
                       "spoken_text": <text>} so the UI can degrade gracefully.
GET  /health           {"status": "ok", "version": ..., "adapters": {...}}
GET  /                 served from frontend/dist if it exists, else a minimal
                       inline placeholder page.

Latency is appended to logs/latency.log (tab-delimited, one summary line per
request) with per-stage breakdown (stt when voice, nlu, merge, search, rank,
compose; tts on /api/tts calls).

Run:
    cd <repo root>
    .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 3000
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime

# --- import bootstrap: allow `uvicorn backend.main:app` from any cwd ---------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles                  # noqa: E402
from pydantic import BaseModel                               # noqa: E402

from database import db                                     # noqa: E402
from memory import conversation                              # noqa: E402
from services import nlu, ranking, response as response_mod, search  # noqa: E402
from services import stt as stt_mod, tts as tts_mod          # noqa: E402

VERSION = "0.3.0"
TOP_N = 3
MAX_TRANSCRIPT = 500  # guard against runaway input

FRONTEND_DIST = os.path.join(ROOT, "frontend", "dist")
LOGS_DIR = os.path.join(ROOT, "logs")
LATENCY_LOG = os.path.join(LOGS_DIR, "latency.log")

_LOGGER = logging.getLogger("vehiclevoice")
_STORE = conversation.SessionStore()
_LATENCY_LOCK = threading.Lock()

app = FastAPI(title="VehicleVoice", version=VERSION)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class VoiceRequest(BaseModel):
    session_id: str = ""
    transcript: str = ""


class TTSRequest(BaseModel):
    session_id: str = ""
    text: str = ""


# ---------------------------------------------------------------------------
# Latency logger
# ---------------------------------------------------------------------------
_STAGE_ORDER = ("stt", "nlu", "merge", "search", "rank", "compose", "tts")


def log_latency(session_id: str, transcript: str, timings: dict) -> None:
    """Append one tab-delimited summary line (per-stage + total) to latency.log.

    Stages are emitted in pipeline order for whichever keys are present, so the
    voice path (stt) and the TTS side-call (tts) fit the same log shape.
    """
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        stages = "\t".join(
            f"{name}={timings[name]:.1f}ms" for name in _STAGE_ORDER if name in timings
        )
        total = sum(v for k, v in timings.items() if k != "total")
        row = (
            f"{datetime.now().isoformat(timespec='milliseconds')}"
            f"\t{session_id}\t{transcript}"
            + (f"\t{stages}" if stages else "")
            + f"\ttotal={total:.1f}ms\n"
        )
        with _LATENCY_LOCK, open(LATENCY_LOG, "a") as fh:
            fh.write(row)
    except OSError:
        _LOGGER.warning("latency log write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
async def run_pipeline(session_id: str, transcript: str, stt_ms: float = 0.0) -> dict:
    """Execute the full search pipeline for one turn. Returns the API payload.

    stt_ms: wall time of the speech-to-text stage when this turn arrived as
    audio (0.0 for typed transcripts) -- included in the reported total.
    """
    state = _STORE.get(session_id)
    timings: dict = {"stt": stt_ms, "nlu": 0.0, "merge": 0.0, "search": 0.0,
                     "rank": 0.0, "compose": 0.0, "tts": 0.0}

    t0 = time.perf_counter()

    t = time.perf_counter()
    parsed = nlu.extract_slots(transcript)
    timings["nlu"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    conversation.apply_parse(state, parsed)
    state.history.append(transcript)
    timings["merge"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    matches = search.search(slots=state.slots)
    timings["search"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    top = ranking.top_n(matches, n=TOP_N, budget=state.slots.get("budget"))
    timings["rank"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    spoken = response_mod.compose_response(top, state.slots)
    timings["compose"] = (time.perf_counter() - t) * 1000

    # Generate TTS audio if edge-tts is available
    audio_url = None
    if tts_mod.is_configured():
        try:
            t = time.perf_counter()
            mp3_bytes = await tts_mod.synthesize_async(spoken)
            tts_ms = (time.perf_counter() - t) * 1000
            timings["tts"] = tts_ms
            
            # Save audio to a temporary file and return URL
            audio_filename = f"{session_id}_{int(t0 * 1000)}.mp3"
            audio_path = os.path.join(ROOT, "static", "audio", audio_filename)
            os.makedirs(os.path.dirname(audio_path), exist_ok=True)
            with open(audio_path, "wb") as f:
                f.write(mp3_bytes)
            
            # Audio URL relative to frontend
            audio_url = f"/audio/{audio_filename}"
        except tts_mod.TTSError as exc:
            _LOGGER.warning("tts failed: %s", exc)
            # Continue without audio; spoken text is still returned

    timings["total"] = (time.perf_counter() - t0) * 1000 + stt_ms

    log_latency(session_id, transcript, timings)

    return {
        "session_id": session_id,
        "transcript": transcript,
        "slots": dict(state.slots),
        "selected_vehicle": state.selected_vehicle,
        "results": top,
        "matched_count": len(matches),
        "spoken": spoken,
        "audio_url": audio_url,
        "latency_ms": timings,
    }


# ---------------------------------------------------------------------------
# Voice audio -> STT -> pipeline (shared by /api/voice multipart & /api/voice/audio)
# ---------------------------------------------------------------------------
async def _voice_audio_turn(session_id: str, audio: UploadFile) -> dict:
    sid = (session_id or "").strip() or str(uuid.uuid4())
    if not stt_mod.is_available():
        return JSONResponse(
            status_code=503,
            content={"status": "stt_unavailable",
                     "message": "faster-whisper is not installed on the server"},
        )
    try:
        audio_bytes = await audio.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"could not read audio upload: {exc!r}")

    try:
        t = time.perf_counter()
        stt_out = stt_mod.transcribe(audio_bytes)
        stt_ms = (time.perf_counter() - t) * 1000
    except stt_mod.STTError as exc:
        _LOGGER.warning("stt failed: %s", exc)
        raise HTTPException(status_code=400,
                            detail={"status": "stt_error", "message": str(exc)})

    transcript = (stt_out.get("text") or "").strip()[:MAX_TRANSCRIPT]
    if not transcript:
        raise HTTPException(status_code=400,
                            detail={"status": "no_speech",
                                    "message": "speech-to-text returned an empty transcript"})

    try:
        payload = await run_pipeline(sid, transcript, stt_ms=stt_ms)
    except Exception as exc:  # keep the API up; surface a clean error
        _LOGGER.exception("pipeline failed for session %s", sid)
        raise HTTPException(status_code=500, detail=f"pipeline error: {exc!r}")

    # Honest STT metadata alongside the pipeline payload.
    payload["stt"] = {
        "model": stt_out.get("model"),
        "language": stt_out.get("language"),
        "audio_duration_s": stt_out.get("duration"),
        "latency_ms": round(stt_ms, 1),
    }
    return payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": VERSION,
        "adapters": {
            "stt": stt_mod.is_available(),
            "tts": tts_mod.is_configured(),
        },
    }


@app.post("/api/voice")
async def voice(request: Request):
    """Typed transcript (JSON) or mic audio (multipart) -- same response shape."""
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/"):
        form = await request.form()
        audio = form.get("audio")
        if audio is None or isinstance(audio, str):
            raise HTTPException(status_code=400,
                                detail={"status": "stt_error",
                                        "message": "multipart field 'audio' is required"})
        return await _voice_audio_turn(str(form.get("session_id") or ""), audio)

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {exc!r}")
    req = VoiceRequest(**body)
    session_id = req.session_id.strip() or str(uuid.uuid4())
    transcript = (req.transcript or "").strip()[:MAX_TRANSCRIPT]
    try:
        return await run_pipeline(session_id, transcript)
    except Exception as exc:  # keep the API up; surface a clean error
        _LOGGER.exception("pipeline failed for session %s", session_id)
        raise HTTPException(status_code=500, detail=f"pipeline error: {exc!r}")


@app.post("/api/voice/audio")
async def voice_audio(session_id: str = Form(""), audio: UploadFile = File(...)):
    """Mic upload path used by the React push-to-talk UI (multipart form)."""
    return await _voice_audio_turn(session_id, audio)


@app.post("/api/tts")
async def tts(req: TTSRequest):
    """Synthesize the spoken answer to mp3.

    Honest-fallback contract (services/tts.py): on any synthesis failure the
    caller gets 200 JSON {"status": "tts_unavailable", "spoken_text": ...} --
    never fake audio -- so the UI can keep showing the text.
    """
    text = (req.text or "").strip()[:tts_mod.MAX_TEXT]
    if not text:
        raise HTTPException(status_code=400,
                            detail={"status": "tts_error", "message": "empty text"})
    if not tts_mod.is_configured():
        return JSONResponse({"status": "tts_unavailable", "spoken_text": text,
                             "message": "edge-tts is not installed on the server"})

    t = time.perf_counter()
    try:
        mp3 = await tts_mod.synthesize_async(text)
    except tts_mod.TTSError as exc:
        _LOGGER.warning("tts unavailable: %s", exc)
        return JSONResponse({"status": "tts_unavailable", "spoken_text": text,
                             "message": str(exc)})
    tts_ms = (time.perf_counter() - t) * 1000
    log_latency(req.session_id.strip() or "tts", f"[tts] {text[:60]}", {"tts": tts_ms})

    return Response(content=mp3, media_type="audio/mpeg",
                    headers={"Content-Length": str(len(mp3)),
                             "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Static frontend (mount only if a build exists; else placeholder page)
# ---------------------------------------------------------------------------
_PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>VehicleVoice</title>
<style>
  body { margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto,
         sans-serif; background: #0f172a; color: #e2e8f0;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; }
  main { max-width: 640px; padding: 2rem; text-align: center; }
  h1 { font-size: 2.2rem; margin: 0 0 .5rem; color: #ffffff; }
  p { line-height: 1.6; color: #94a3b8; }
  .badge { display: inline-block; margin-top: 1.2rem; padding: .4rem 1rem;
           border-radius: 999px; background: #1e293b; color: #38bdf8;
           font-size: .85rem; border: 1px solid #334155; }
</style>
</head>
<body>
<main>
  <h1>VehicleVoice</h1>
  <p>Voice-driven used-vehicle search. Say “mini truck under 5 lakh, CNG,
     Mumbai” and hear the top matches — every number straight from the catalog.</p>
  <p>The push-to-talk React frontend lands in a later milestone.
     The API is live at <code>/api/voice</code> and health at <code>/health</code>.</p>
  <span class="badge">backend v{VERSION} running</span>
</main>
</body>
</html>
"""


def _placeholder(request: Request) -> HTMLResponse:  # pragma: no cover - trivial
    return HTMLResponse(_PLACEHOLDER_HTML)


if os.path.isdir(FRONTEND_DIST):
    # Real React build exists: serve it (registration last so /api wins).
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    app.get("/")(_placeholder)

# Static audio files for TTS playback
AUDIO_DIR = os.path.join(ROOT, "static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")


# Make the SQLite catalog exist (idempotent) at import time so the app is
# immediately usable.
db.ensure_catalog()