# Voicebot — Live (Streaming) Architecture

This folder is the **live-transcription update** of the original
`Google-Meet-Bot-sarvam` project. The batch pipeline (record the whole meeting,
then transcribe when it ends) is replaced with a **streaming pipeline**: audio is
transcribed *while the meeting is happening*, partials appear live in the UI, and
final utterances feed an LLM that fills in structured fields (a form).

Everything already working in the batch project is **kept** — only the transport
and transcription layer changed.

---

## 1. Before vs. after

**Before (batch):**

```
Google Meet → bot joins → WebRTC capture → PCM 16kHz → AudioRecorder → WAV
→ MEETING ENDS → Sarvam Batch job (upload, wait) → transcript
```

**After (live):**

```
                      Chrome
                         │
                         ▼
                WebRTC Remote Audio
                         │
                         ▼
                   PCM 16 kHz
                         │
              ┌──────────┴───────────┐
              ▼                      ▼
       Recording path           Live path
        (WAV on disk)        (WebSocket → FastAPI → Sarvam streaming STT)
                                              │
                                partial ──────└────── final
                                   │                 │
                                   ▼                 ▼
                             Live partial      committed segment
                                                   │
                                            debounce + LLM extract
                                                   │
                                                   ▼
                                              fields → MongoDB → React UI
```

The two key invariants from the batch project hold here too:

1. **Recording path is kept.** The WAV is still written second-by-second to
   `assets/recordings/<session_id>.wav`. If streaming STT dies for any reason the
   raw audio is untouched and can be re-transcribed with the batch API.
2. **Echo-free by construction.** `meet_audio_capture.js` only feeds a silent
   sink; the bot's Chrome is `--mute-audio` with a silent virtual mic.

---

## 2. Component map

| Component | File | Responsibility |
|---|---|---|
| Configuration | `config.py` | `.env`, Sarvam/LLM/Mongo/transport settings |
| Config task | `.env.example` | Template for all settings |
| Meet automation | `utils/meet_bot.py` | Chrome launch, anonymous join, in-meeting detection (unchanged logic) |
| In-page capture | `utils/meet_audio_capture.js` | WebRTC remote-track mixer → 16 kHz PCM chunks (unchanged) |
| Dual-path recorder | `utils/audio_recorder.py` | Polls `__meetRec.drain()`, writes WAV **and** pushes chunks to `/ws/audio` |
| Sarvam streaming client | `utils/sarvam_streaming.py` | Hand-rolled WebSocket client for `saaras:v3-realtime` (true partials) or legacy `saaras:v3/v4` |
| Transcript engine | `utils/transcript_manager.py` | Partial vs committed-final state; extraction cursor |
| LLM field extractor | `utils/llm_extractor.py` | Debounced, tail-only extraction; OpenAI-compatible + regex fallback |
| Storage | `utils/storage.py` | MongoDB, or JSON files under `data/sessions/` |
| API server | `utils/server_app.py` | REST control plane + `/ws/audio` relay + `/ws/session/<id>` broadcast |
| Entry point | `run_server.py` | `uvicorn utils.server_app:app` |
| React UI | `frontend/` | Vite + React, live WebSocket updates |
| Design notes | `docs/LIVE_ARCHITECTURE.md` | This document |

---

## 3. The live flow, step by step

### 3.1 Start a session
`POST /api/sessions/start {"meeting_url": "https://meet.google.com/…"}`

1. A `SessionState` is created with a session id (`YYYYMMDD_HHMMSS`), a
   `TranscriptManager`, a `FieldExtractor`, and is persisted as `joining`.
2. A daemon thread (`_join_worker`) runs `GoogleMeetBot`: joins anonymously,
   waits to be admitted, then `start_recording(session_id)`.
3. UI clients connect to `WS /ws/session/<id>` and immediately receive a
   `snapshot` (status, transcript, fields).

### 3.2 Per-second audio chunk
Inside `StreamingAudioRecorder._run` (a polling thread, 1s cadence):

```
window.__meetRec.drain()  →  base64 chunk (≈1s of PCM)
        │
        ├──────────────► WAV file (recording path, always kept)
        │
        └──────────────► WS "bind" session_id
                          │
                          ▼
                  WS /ws/audio   (this is the "WebSocket → backend" leg)
                          │
                          ▼
                 SarvamStream.open()
                          │ "ready"
                          ▼
         {"event":"audio_input","audio":"<base64>"}  (realtime endpoint)
```

If `/ws/audio` can't connect or Sarvam rejects the socket, the recorder logs the
error and **keeps recording WAV only** — nothing else breaks.

### 3.3 Events from Sarvam
A pump coroutine reads Sarvam messages and turns them into UI events + manager
state:

| Sarvam event | Manager action | UI event |
|---|---|---|
| `session.begin` | mark connection time | – |
| `vad.speech_start` / `speech_end` | remember speech start | `{"type":"vad"}` |
| `transcript.partial` | replace `partial` text | `{"type":"partial"}` |
| `transcript.final` | commit a segment, schedule extraction | `{"type":"final","text","start","end"}` |
| `error` | record error | `{"type":"stt_error"}` |
| `session.end` | finalize timestamps + duration | `{"type":"session_end",...}` |

> **Why the manager exists.** Sarvam can emit the same growing utterance many
> times as partials (`"My…"`, `"My name is…"`, …). We only keep the latest
> partial (one floating line in the UI) and only **commit** once `transcript.final`
> arrives — so the store and the LLM never see duplicated or half-baked text.

### 3.4 LLM field extraction — debounced and tail-only
`FieldExtractor` receives a "schedule" nudge on every committed final. It waits
`LLM_DEBOUNCE_SECONDS` of quiet, then extracts **only the unconsumed tail** of the
committed transcript (`TranscriptManager.pending_text()` / `mark_extracted()`).

```
partials:   "My…" → "My name is…" → "My name is Prateek…"
final:      "My name is Prateek Kumar."          ← commit
(1.5s quiet)
LLM:        [{"field":"full_name","value":"Prateek Kumar","confidence":0.98}]
```

This matches the goal of **not firing an LLM call per partial** and **not
re-sending the whole meeting** each time.

- `LLM_API_KEY` set → calls an OpenAI-compatible `/chat/completions` endpoint and
  parses the JSON array `[{field,value,confidence}]`.
- `LLM_API_KEY` empty → built-in regex fallback (`email`, `phone`) so the demo
  still works offline. The schema itself is configurable via `LLM_FIELD_SCHEMA`.
- Fields are deduplicated, persisted, and broadcast as `{"type":"fields"}`.

### 3.5 Stop a session
`POST /api/sessions/<id>/stop`

1. Status → `stopping`.
2. The recorder thread is told to stop: final browser drain, WAV header
   finalized, `{"event":"end"}` sent over `/ws/audio`.
3. The relay forwards `end` to Sarvam; Sarvam flushes final transcripts and
   returns `session.end`; the relay closes the Sarvam socket.
4. `FieldExtractor.flush_now()` + `wait_idle()` extracts anything left.
5. Status → `done`; full document persisted; `{"type":"done"}` broadcast.

---

## 4. Message protocols

### 4.1 `/ws/audio` (bot recorder → server)
```
bind  {"event":"bind",  "session_id":"20260925_103000"}
audio {"event":"audio", "audio":"<base64 PCM>"}        (every ~1s)
end   {"event":"end"}
←     {"event":"ready"}   after Sarvam socket is open
←     {"event":"error"}   if Sarvam could not open
```

### 4.2 `/ws/session/<id>` (server → UI)
```
snapshot    full current document (status, segments, partial, fields)
status      {status: "joining|recording|stopping|done|failed", error?}
partial     {text, language?}
final       {text, start, end, language?}
vad         {state: "speech_start"|"speech_end"}
fields      [{field, value, confidence}]
stt_error   {message, fatal}
session_end {audio_duration_s}
done        {session: full document}
```

### 4.3 Sarvam streaming ("realtime", default)
```
URL    wss://api.sarvam.ai/speech-to-text-realtime/ws
→      {"event":"audio_input","audio":"<base64 linear16 pcm>"}
→      {"event":"ping"}                              (keepalive)
→      {"event":"end"}                               (graceful close)
←      session.begin | vad.speech_start | vad.speech_end |
       transcript.partial | transcript.final | session.end | error
```

Raw 16 kHz mono 16-bit PCM (linear16) is sent — exactly the format the Meet page
already emits — so **no WAV framing or ffmpeg is required** for live audio.

---

## 5. Configuration reference

| Key | Default | Notes |
|---|---|---|
| `SARVAM_API_KEY` | *(required)* | Streams, unlike batch, have no per-job credentials |
| `SARVAM_STREAMING_ENDPOINT` | `realtime` | `realtime` (true partials) \| `legacy` (final per utterance) |
| `SARVAM_STREAMING_MODEL` | `saaras:v3-realtime` | realtime endpoint also accepts `saaras:v4`; legacy accepts `saaras:v3`/`saaras:v4` |
| `SARVAM_STREAM_TYPE` | `balanced` | `fast` = lowest partial latency, `simulated` = no partials |
| `SARVAM_STREAMING_MODE` | `transcribe` | `transcribe`/`translate`/`verbatim`/`translit`/`codemix` (final text only) |
| `SARVAM_LANGUAGE_CODE` | `auto` | auto-detect; or e.g. `en-IN`, `hi-IN`, `te-IN` |
| `SARVAM_VAD_THRESHOLD` | `0.3` | VAD sensitivity (realtime) |
| `SARVAM_SILENCE_DURATION_MS` | `500` | end-of-turn silence (realtime) |
| `SARVAM_MIN_SPEECH_DURATION_MS` | `250` | minimum speech to count as a turn |
| `SARVAM_PING_INTERVAL` | `10` | keepalive to avoid `1008` inactivity close |
| `LLM_EXTRACTION_ENABLED` | `true` | master switch for field extraction |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | OpenAI-compatible | empty key → regex fallback |
| `LLM_DEBOUNCE_SECONDS` | `1.5` | pause-batching of consecutive finals |
| `LLM_FIELD_SCHEMA` | form schema | instructs the LLM which fields to extract |
| `MONGO_URI` / `MONGO_DB` / `MONGO_COLLECTION` | empty / `voicebot` / `sessions` | empty → JSON files in `data/sessions/` |
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `8000` | binds locally only |
| `AUDIO_WS_URL` | `ws://127.0.0.1:8000/ws/audio` | how the bot reaches the relay |
| `AUTO_CLICK_JOIN` / `BOT_DISPLAY_NAME` / `EPHEMERAL_PROFILE` | batch defaults | join behaviour, unchanged |

---

## 6. What was deliberately kept, changed, added

**Kept (from the batch project):**
- `meet_audio_capture.js` — WebRTC remote-track mixing, PCM encoding, chunking.
- `utils/meet_bot.py` — Chrome automation, anonymous guest join, muting, WAV
  assembly pattern.
- WAV recording path as the raw source of truth.

**Changed:**
- Audio transport: chunks now also go over a WebSocket to the server (and on to
  Sarvam's streaming socket) instead of waiting for the meeting to end.
- Transcription: batch job → streamed final segments with live partials.

**Added:**
- Sarvam streaming client (realtime + legacy endpoints).
- Transcript manager (partial/final, extraction cursor).
- Debounced LLM field extractor (OpenAI-compatible + regex fallback).
- MongoDB/JSON session store.
- React UI with live WebSocket updates.