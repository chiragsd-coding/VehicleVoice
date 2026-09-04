// audio.js — push-to-talk recording helpers (MediaRecorder -> WAV).
//
// Browser MediaRecorder produces webm/opus, not WAV, so we capture with
// MediaRecorder and then transcode to 16-bit PCM WAV via decodeAudioData.
// `startRecording(onTick)` begins capturing; `stopRecording()` returns a
// Promise<Blob> of the WAV. This module is framework-free so the later STT
// wiring only has to consume the resulting Blob.

export function isRecordingSupported() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
    typeof MediaRecorder !== 'undefined')
}

let recorder = null
let stream = null
let chunks = []
let tickTimer = null

export async function startRecording(onTick = () => {}) {
  if (!isRecordingSupported()) {
    throw new Error('Microphone recording not supported in this browser')
  }
  stream = await navigator.mediaDevices.getUserMedia({ audio: true })
  // Use MediaRecorder without specifying codec - Firefox may not support codec spec
  recorder = new MediaRecorder(stream)
  chunks = []
  
  // Handle dataavailable with proper null check
  recorder.ondataavailable = (e) => {
    console.log('ondataavailable called, size:', e.data?.size)
    if (e.data && e.data.size > 0) {
      chunks.push(e.data)
    }
  }
  
  recorder.start()
  let secs = 0
  tickTimer = setInterval(() => { secs += 0.1; onTick(secs) }, 100)
}

export async function stopRecording() {
  if (!recorder) throw new Error('No active recording')
  if (tickTimer) clearInterval(tickTimer)
  const rec = recorder
  recorder = null
  return new Promise((resolve, reject) => {
    let stopped = false
    
    // Wait for data to be flushed before stopping
    const flushAndStop = () => {
      if (stopped) return
      stopped = true
      
      setTimeout(() => {
        try {
          rec.stop()
        } catch (err) {
          stopTracks()
          reject(err)
        }
      }, 50) // Give data time to flush
    }
    
    rec.onstop = async () => {
      try {
        // Use the actual MIME type from the recorder if available
        const mimeType = rec.mimeType || 'audio/webm'
        const raw = new Blob(chunks, { type: mimeType })
        console.log('Recording stopped, MIME type:', mimeType, 'Blob size:', raw.size, 'Chunks:', chunks.length)
        resolve(await toWav(raw, mimeType))
      } catch (err) {
        console.error('Failed to convert recording:', err)
        reject(err)
      } finally {
        stopTracks()
      }
    }
    
    rec.onerror = (e) => {
      console.error('Recorder error:', e)
      if (!stopped) {
        stopped = true
        stopTracks()
      }
      reject(e.error || new Error('recorder error'))
    }
    
    // Use a timeout to ensure we stop even if data doesn't flush
    setTimeout(() => {
      if (!stopped) flushAndStop()
    }, 300) // Stop after 300ms of inactivity
    
    flushAndStop()
  })
}

export function cancelRecording() {
  if (tickTimer) clearInterval(tickTimer)
  if (recorder && recorder.state !== 'inactive') {
    try { recorder.onstop = null; recorder.stop() } catch { /* ignore */ }
  }
  recorder = null
  stopTracks()
}

function stopTracks() {
  if (stream) {
    stream.getTracks().forEach((t) => t.stop())
    stream = null
  }
  chunks = []
}

// ---- WAV transcoding -----------------------------------------------------
async function toWav(blob, mimeType = 'audio/webm') {
  const AudioContext = window.AudioContext || window.webkitAudioContext
  if (!AudioContext) throw new Error('AudioContext unavailable')
  const ctx = new AudioContext()
  
  try {
    const arrayBuffer = await blob.arrayBuffer()
    const audioBuffer = await ctx.decodeAudioData(arrayBuffer)
    const numCh = Math.min(2, audioBuffer.numberOfChannels)
    const sampleRate = audioBuffer.sampleRate
    const len = audioBuffer.length
    const interleaved = new Float32Array(len * numCh)
    for (let ch = 0; ch < numCh; ch++) {
      const data = audioBuffer.getChannelData(ch)
      for (let i = 0; i < len; i++) interleaved[i * numCh + ch] = data[i]
    }
    return encodeWav(interleaved, numCh, sampleRate)
  } catch (err) {
    throw err
  }
  // Don't close ctx - the browser will clean it up eventually
  // Closing it immediately causes "detached buffer" errors
}

function encodeWav(samples, numChannels, sampleRate) {
  const bytesPerSample = 2
  const blockAlign = numChannels * bytesPerSample
  const dataSize = samples.length * bytesPerSample
  const buffer = new ArrayBuffer(44 + dataSize)
  const view = new DataView(buffer)
  const writeStr = (offset, str) => {
    for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i))
  }
  writeStr(0, 'RIFF')
  view.setUint32(4, 36 + dataSize, true)
  writeStr(8, 'WAVE')
  writeStr(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, numChannels, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * blockAlign, true)
  view.setUint16(32, blockAlign, true)
  view.setUint16(34, bytesPerSample * 8, true)
  writeStr(36, 'data')
  view.setUint32(40, dataSize, true)
  let offset = 44
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true)
    offset += 2
  }
  return new Blob([buffer], { type: 'audio/wav' })
}