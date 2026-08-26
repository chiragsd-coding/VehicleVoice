// api.js — thin client for the VehicleVoice FastAPI backend.
//
// Endpoints
//   GET  /health          backend liveness
//   POST /api/voice       {session_id, transcript} -> full pipeline payload
//   POST /api/voice/audio multipart (session_id, audio) -> same payload shape;
//                         handled by STT once wired. 404 today => caller shows
//                         a graceful "voice not wired yet" notice.
//
// All paths are relative so the same client works under the Vite dev proxy
// and when the backend serves frontend/dist in production.

const VOICE_ENDPOINT = '/api/voice'
const AUDIO_ENDPOINT = '/api/voice/audio'
const HEALTH_ENDPOINT = '/health'

const SESSION_KEY = 'vehiclevoice.session_id'

export function loadSessionId() {
  let id = localStorage.getItem(SESSION_KEY)
  if (!id) {
    id = newSessionId()
    localStorage.setItem(SESSION_KEY, id)
  }
  return id
}

export function newSessionId() {
  const id =
    typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID()
      : 'sess-' + Math.random().toString(36).slice(2) + Date.now().toString(36)
  localStorage.setItem(SESSION_KEY, id)
  return id
}

export async function fetchHealth() {
  const res = await fetch(HEALTH_ENDPOINT)
  if (!res.ok) throw new Error(`health check failed: HTTP ${res.status}`)
  return res.json()
}

async function parseError(res) {
  let detail = ''
  try {
    const body = await res.json()
    detail = body && body.detail ? String(body.detail) : ''
  } catch {
    /* non-JSON error body */
  }
  return detail ? `HTTP ${res.status}: ${detail}` : `HTTP ${res.status}`
}

export async function postVoice(payload) {
  const res = await fetch(VOICE_ENDPOINT, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new Error(await parseError(res))
  return res.json()
}

// Audio upload — the future STT path. Sends the recorded WAV plus the
// session id as multipart/form-data. Returns the same payload shape as
// /api/voice once the backend STT endpoint exists; throws on 404 today.
export async function postVoiceAudio(sessionId, wavBlob) {
  const form = new FormData()
  form.append('session_id', sessionId)
  form.append('audio', wavBlob, 'recording.wav')
  const res = await fetch(AUDIO_ENDPOINT, { method: 'POST', body: form })
  if (!res.ok) throw new Error(await parseError(res))
  return res.json()
}