# Voicebot — Live (streaming) Meet transcription + form extraction

Real-time version of the Google Meet bot. The bot joins a Meet anonymously,
captures the other participants' audio inside its own Chrome (echo-free), and —
*while the meeting is still running* — streams that audio into **Sarvam's
streaming speech-to-text**, shows **live partial transcripts** in a React UI,
commits each **final utterance**, and runs a **debounced LLM** pass to fill in
structured form fields. Everything is stored in **MongoDB** (or JSON files) and
the raw WAV is still saved as the source of truth.

```
Chrome bot → WebRTC → PCM 16 kHz
              ├──────────► WAV file (recording path, kept)
              └──────────► WebSocket → FastAPI → Sarvam streaming STT
                                         ├─► partials  → live UI
                                         └─► finals    → LLM → fields → store → UI
```

## Quick start

```
# 1. environment + deps
python -m venv venv ; venv\Scripts\activate       (Windows)
pip install -r requirements.txt

# 2. configure
copy .env.example .env      # set SARVAM_API_KEY (+ optional LLM/Mongo settings)

# 3. run the server
python run_server.py        # FastAPI on http://127.0.0.1:8000

# 4. (optional) run the React UI next to it
cd frontend
npm install
npm run dev                 # http://localhost:5173  (proxies /api and /ws)
```

Open `http://127.0.0.1:8000` (or the Vite dev server). Enter a Meet URL, click
**Start**, admit the bot into the meeting in its own Chrome window, and watch the
transcript + fields fill in live. Click **Stop** to finalize.

## Running entirely without a backend API key (offline demo)

- Set `LLM_API_KEY=` (leave empty) → a built-in regex extractor handles email/phone.
- If `SARVAM_API_KEY` is missing, the live leg reports the error but the **WAV
  recording still happens**, and `GET /api/sessions/<id>` still serves the session.

## Key entry points

| What | Where |
|---|---|
| FastAPI app + all endpoints | `utils/server_app.py` |
| Sarvam streaming client | `utils/sarvam_streaming.py` |
| Debounced LLM extractor | `utils/llm_extractor.py` |
| Live transcript state | `utils/transcript_manager.py` |
| Dual-path recorder (WAV + WS) | `utils/audio_recorder.py` |
| React live UI | `frontend/` |
| Full design notes | `docs/LIVE_ARCHITECTURE.md` |

## Laws of the system

1. **Recording path is never dropped** — the WAV is the raw source of truth if
   streaming transcription fails.
2. **Never call the LLM per partial** — extraction is debounced and only sees the
   committed transcript tail.
3. **No echo** — the bot's audio graph only feeds a silent sink; Chrome is muted
   with a silent virtual microphone.

## Note on consent

Like the original bot, this joins meetings as a visible participant and records.
Make sure you have the right to record everyone on the call.