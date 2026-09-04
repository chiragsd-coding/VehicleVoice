import React, { useCallback, useEffect, useRef, useState } from 'react'
import { fetchHealth, loadSessionId, newSessionId, postVoice, postVoiceAudio } from './api.js'
import { isRecordingSupported, startRecording, stopRecording, cancelRecording } from './audio.js'

// ---- formatting helpers (mirror the backend's response.py formatting) ----
function formatLakh(price) {
  const lakh = price / 100000
  if (lakh >= 10) return `₹${lakh.toFixed(1)} lakh`
  const s = lakh.toFixed(1)
  return `₹${s.endsWith('.0') ? s.slice(0, -2) : s} lakh`
}
function fmtKm(km) {
  return Number(km).toLocaleString('en-IN')
}
const SLOT_LABELS = {
  budget: 'Budget',
  fuel: 'Fuel',
  body_type: 'Body type',
  city: 'City',
  purpose: 'Purpose',
}
function formatSlotValue(key, value) {
  if (value === null || value === undefined || value === '') return null
  if (key === 'budget') return formatLakh(Number(value))
  return String(value).replace(/_/g, ' ')
}

export default function App() {
  const [sessionId, setSessionId] = useState(loadSessionId)
  const [input, setInput] = useState('')
  const [response, setResponse] = useState(null) // latest pipeline payload
  const [turnHistory, setTurnHistory] = useState([]) // older payloads, newest first
  const [historyLoaded, setHistoryLoaded] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [backend, setBackend] = useState(null) // health payload or false/error string
  const [micState, setMicState] = useState('idle') // idle | denied | recording | converting
  const [recSecs, setRecSecs] = useState(0)
  const [speechUrl, setSpeechUrl] = useState(null) // reserved for TTS output
  const micSupported = useRef(isRecordingSupported())

  // Backend liveness on load (non-blocking).
  useEffect(() => {
    fetchHealth()
      .then((h) => setBackend(h))
      .catch((err) => setBackend(`unreachable (${err.message})`))
  }, [])

  // --- session -----------------------------------------------------------
  const resetSession = () => {
    setSessionId(newSessionId())
    setResponse(null)
    setTurnHistory([])
    setHistoryLoaded(false)
    setInput('')
    setSpeechUrl(null)
    setError(null)
  }

  // --- submit a typed transcript -----------------------------------------
  const submit = useCallback(async (text) => {
    const transcript = (text ?? input).trim()
    if (!transcript || loading) return
    setLoading(true)
    setError(null)
    try {
      const data = await postVoice({ session_id: sessionId, transcript })
      setResponse(data)
      setTurnHistory((h) => [data, ...h])
      if (data.audio_url) setSpeechUrl(data.audio_url) // TTS lands later
    } catch (err) {
      setError(`Request failed: ${err.message}`)
    } finally {
      setLoading(false)
    }
  }, [input, loading, sessionId])

  // --- push-to-talk: voice capture ---------------------------------------
  // Records while held; on release the WAV is POSTed to /api/voice/audio,
  // the endpoint the backend STT wiring will implement. Until then a 404
  // shows a graceful notice and the app stays fully usable via typed text.
  const micPointer = useRef(0)

  async function handleMicDown(e) {
    e.preventDefault()
    if (micState === 'recording' || micState === 'converting' || loading) return
    micPointer.current = e.pointerId
    setError(null)
    setRecSecs(0)
    try {
      await startRecording((secs) => setRecSecs(secs))
      setMicState('recording')
    } catch (err) {
      setMicState('denied')
      setError(`Microphone unavailable: ${err.message}. Type your query below.`)
    }
  }

  async function handleMicUp(e) {
    if (e.pointerId !== micPointer.current) return
    if (micState !== 'recording') return
    setMicState('converting')
    try {
      const wav = await stopRecording()
      setRecSecs(0)
      console.log('WAV blob size:', wav.size, 'type:', wav.type)
      await uploadWav(wav)
    } catch (err) {
      console.error('Voice recording failed:', err)
      setError(`Voice capture failed: ${err.message}. Type your query below.`)
      setMicState('idle')
    }
  }

  async function uploadWav(wav) {
    setLoading(true)
    try {
      const data = await postVoiceAudio(sessionId, wav)
      setResponse(data)
      setTurnHistory((h) => [data, ...h])
      // audio_url will be set if TTS succeeded, otherwise spoken text is still shown
      if (data.audio_url) setSpeechUrl(data.audio_url)
      else setSpeechUrl(null)
    } catch (err) {
      setMicState('idle')
      setError(
        'Voice audio was recorded but speech-to-text is not wired to the backend yet. ' +
          `(${err.message}) Type your query below — the mic sends WAV to /api/voice/audio ` +
          'ready for the STT milestone.',
      )
    } finally {
      setLoading(false)
      setMicState('idle')
    }
  }

  function handleMicCancel(e) {
    if (e.pointerId !== micPointer.current) return
    if (micState === 'recording') {
      cancelRecording()
      setMicState('idle')
      setRecSecs(0)
    }
  }

  // --- render -------------------------------------------------------------
  return (
    <div className="page">
      <header className="header">
        <h1>VehicleVoice</h1>
        <p className="tagline">
          Voice-driven used-vehicle search · every number straight from the catalog
        </p>
        <div className="meta-row">
          <span className={`health ${backend && backend.status ? 'ok' : 'bad'}`}>
            backend: {backend && backend.status ? `v${backend.version}` : String(backend)}
          </span>
          <span className="session" title="This id keeps multi-turn conversation memory across turns in this browser">
            session: <code>{sessionId.slice(0, 8)}…</code>
          </span>
          <button className="link-button" onClick={resetSession} title="Start a fresh conversation (new session id, clears memory)">
            new session
          </button>
        </div>
      </header>

      <main>
        <section className="input-section card">
          {micSupported.current ? (
            <div className="ptt-row">
              <button
                type="button"
                className={`mic-btn ${micState === 'recording' ? 'recording' : ''} ${micState === 'converting' ? 'converting' : ''}`}
                onPointerDown={handleMicDown}
                onPointerUp={handleMicUp}
                onPointerCancel={handleMicCancel}
                onContextMenu={(e) => e.preventDefault()}
                disabled={loading || micState === 'converting'}
                title={
                  micState === 'recording'
                    ? 'Release to send'
                    : 'Hold to talk (speech-to-text lands in a later milestone; release will send the audio to /api/voice/audio)'
                }
              >
                <MicIcon active={micState === 'recording'} />
                <span>
                  {micState === 'recording'
                    ? `Listening… ${recSecs.toFixed(1)}s — release to send`
                    : micState === 'converting'
                      ? 'Converting audio…'
                      : 'Hold to talk'}
                </span>
              </button>
              {micState === 'denied' && (
                <p className="hint warn">Mic access was denied earlier — typed input works fine.</p>
              )}
            </div>
          ) : (
            <p className="hint">This browser has no mic capture — typed input works fine.</p>
          )}

          <div className="input-row">
            <textarea
              rows={2}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  submit()
                }
              }}
              placeholder="e.g. mini truck under 5 lakh, CNG, Mumbai — or a follow-up like “only CNG” or “show the first one”"
              aria-label="Query transcript"
            />
            <button
              type="button"
              className="submit-btn"
              onClick={() => submit()}
              disabled={loading || !input.trim()}
            >
              {loading ? 'Searching…' : 'Search'}
            </button>
          </div>
          {error && <p className="error">{error}</p>}
        </section>

        {response && (
          <>
            <section className="grid-2">
              <div className="card">
                <h2>Transcript</h2>
                <p className="quote">“{response.transcript}”</p>
              </div>
              <div className="card">
                <h2>Spoken response</h2>
                <p className="spoken">{response.spoken}</p>
                <audio
                  controls
                  className="audio-player"
                  src={response.audio_url || undefined}
                  aria-label="Spoken answer playback"
                >
                  <track kind="captions" />
                </audio>
                {!response.audio_url && (
                  <p className="hint">Audio synthesis unavailable; the text above is what will be spoken.</p>
                )}
              </div>
            </section>

            <section className="card">
              <h2>Slot inspector</h2>
              <div className="slots">
                {Object.entries(SLOT_LABELS).map(([key, label]) => {
                  const value = formatSlotValue(key, response.slots?.[key])
                  return (
                    <div className={`slot ${value ? 'set' : 'unset'}`} key={key}>
                      <span className="slot-label">{label}</span>
                      <span className="slot-value">{value ?? 'not set'}</span>
                    </div>
                  )
                })}
              </div>
            </section>

            <section className="card">
              <h2>
                Top matches
                <span className="badge">{response.matched_count} matched</span>
                {response.selected_vehicle && (
                  <span className="badge">selected: #{response.selected_vehicle}</span>
                )}
              </h2>
              {response.results && response.results.length > 0 ? (
                <div className="results">
                  {response.results.map((v, i) => (
                    <ResultCard key={v.id ?? i} v={v} rank={i + 1} />
                  ))}
                </div>
              ) : (
                <p className="hint">No matching vehicles — try a different city, raise the budget, or remove a filter.</p>
              )}
            </section>

            <section className="card latency-card">
              <h2>
                Latency <span className="hint-inline">(per-stage, from latency_ms)</span>
              </h2>
              <div className="latency">
                <LatencyBar name="stt" ms={response.latency_ms?.stt} />
                <LatencyBar name="nlu" ms={response.latency_ms?.nlu} />
                <LatencyBar name="merge" ms={response.latency_ms?.merge} />
                <LatencyBar name="search" ms={response.latency_ms?.search} />
                <LatencyBar name="rank" ms={response.latency_ms?.rank} />
                <LatencyBar name="compose" ms={response.latency_ms?.compose} />
                <LatencyBar name="tts" ms={response.latency_ms?.tts} />
              </div>
              <p className="total">
                total <strong>{(response.latency_ms?.total ?? 0).toFixed(1)} ms</strong>
              </p>
            </section>

            {turnHistory.length > 1 && (
              <section className="card">
                <h2>Earlier turns <span className="hint-inline">(same session id — slots merged across turns)</span></h2>
                <ul className="turns">
                  {turnHistory.slice(1).map((t, i) => (
                    <li key={i}>
                      <span className="turn-q">“{t.transcript}”</span>
                      <span className="turn-count">{t.matched_count} matched</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}

        {!response && !loading && (
          <section className="card placeholder-card">
            <p>
              Try: <em>“mini truck under 5 lakh, CNG, Mumbai”</em> or{' '}
              <em>“pickup truck under 4 lakh”</em>, then refine with{' '}
              <em>“only CNG”</em> or <em>“in Pune”</em>.
            </p>
          </section>
        )}
      </main>

      <footer className="footer">
        <span>session id persists in this browser (localStorage) so follow-up turns merge on the backend.</span>
      </footer>
    </div>
  )
}

// ---- small presentational pieces ------------------------------------------
function ResultCard({ v, rank }) {
  return (
    <div className="result-card">
      <div className="result-head">
        <span className="rank">#{rank}</span>
        <span className="name">
          {v.make} {v.model}
        </span>
        {v.verified ? <span className="badge verified">verified</span> : <span className="badge unverified">papers not verified</span>}
      </div>
      <div className="result-meta">
        <span className="price">{formatLakh(v.price)}</span>
        <span>{v.year} model</span>
        <span>{v.fuel}</span>
        <span>{v.body_type}</span>
        <span>{v.city}</span>
        <span>{fmtKm(v.km)} km</span>
        <span>payload {fmtKm(v.payload_kg)} kg</span>
      </div>
    </div>
  )
}

function LatencyBar({ name, ms, placeholder }) {
  return (
    <div className="latency-row">
      <span className="latency-name">{name}</span>
      <span className="latency-ms">{placeholder ?? `${ms.toFixed(1)} ms`}</span>
    </div>
  )
}

function MicIcon({ active }) {
  return (
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="2" width="6" height="12" rx="3" className={active ? 'mic-live' : ''} />
      <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
      <line x1="12" y1="18" x2="12" y2="22" />
      <line x1="8" y1="22" x2="16" y2="22" />
    </svg>
  )
}