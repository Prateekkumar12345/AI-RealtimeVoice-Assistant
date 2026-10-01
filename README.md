# Voicebot — Live (streaming) Meet transcription + form extraction

Real-time version of the Google Meet bot. The bot signs itself into Google, joins
a Meet under that account, captures the other participants' audio inside its own
Chrome (echo-free), and —
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
copy .env.example .env      # SARVAM_API_KEY + GOOGLE_EMAIL/GOOGLE_PASSWORD for auto sign-in

# 3. run the server
python run_server.py        # FastAPI on http://127.0.0.1:8000

# 4. (optional) run the React UI next to it
cd frontend
npm install
npm run dev                 # http://localhost:5173  (proxies /api and /ws)
```

Open `http://127.0.0.1:8000` (or the Vite dev server). Enter a Meet URL and click
**Start**. The bot opens its own Chrome, signs in by itself, and joins on its own —
no clicking through a login form, and nobody has to admit it. Watch the transcript
+ fields fill in live. Click **Stop** to finalize.

## Automatic Google sign-in

The bot never asks a human to log in. On the first run it types the credentials
from `.env` into Google's sign-in form, then keeps the resulting session in
`bot_session/` — 7 files, ~176 KB, versus the 190 MB a full Chrome profile needs.

```
run 1:  empty vault  -> throwaway profile -> type password -> join -> save session
run 2+: seeded vault -> throwaway profile -> already signed in -> join
```

So the password is typed roughly once a year, when Google's auth cookies finally
expire, rather than once per meeting. Every run still uses a disposable profile;
only the 176 KB of session files persists.

```bash
# one-time: set these in .env
#   GOOGLE_EMAIL=bot@example.com     a DEDICATED account, not a personal one
#   GOOGLE_PASSWORD=...
#   AUTO_LOGIN=true

# check what the vault currently holds (no secrets, safe to run)
venv\Scripts\python.exe -c "from utils import session_vault; print(session_vault.status())"

# force a full re-login (e.g. Google changed something) - the next run retypes the password
rmdir /s /q bot_session
```

Delete `bot_session/` (or sign out of Google) to revoke the stored session. The
password in `.env` is a separate secret and is unaffected.

**Requirements and limits.** This needs a **dedicated Google account with 2FA
off**. Google can still refuse unattended sign-in with a CAPTCHA, an
"unusual traffic" interstitial, or the "This browser or app may not be secure"
block; the bot uses `undetected-chromedriver` to avoid the last of those, but
cannot beat the first two. When Google refuses, the run **fails loudly with a
screenshot** in `assets/join_diagnostics/` rather than hanging — there is no
interactive fallback by design.

## Running entirely without a backend API key (offline demo)

- Set `LLM_API_KEY=` (leave empty) → a built-in regex extractor handles email/phone.
- If `SARVAM_API_KEY` is missing, the live leg reports the error but the **WAV
  recording still happens**, and `GET /api/sessions/<id>` still serves the session.

## Identifying who is speaking

Meet mixes every participant into a single audio stream, so the bot cannot just
read the speaker out of the transcript. Instead the page taps each remote
track's audio *before* the mix, measures voice activity per person, and numbers
participants in the order they are first heard. Each committed transcript is then
matched to whichever activity window overlaps it most, and labelled
`Speaker 1`, `Speaker 2`, and so on. The UI shows a live speaker count and each
line carries its own colour.

This keeps **one** speech-to-text stream for the whole call. The alternative —
one stream per person — would multiply latency and cost, and would still merge
anyone talking at the same time. The trade-off is honest: if two people talk over
each other for a whole sentence, that sentence goes to whoever held the floor
longest.

A transcript can arrive from Sarvam *before* the browser reports the activity for
that moment, so unattributed lines are held and re-labelled as soon as the
matching activity turns up. Nothing is dropped and no line is ever guessed.

Numbers follow first **voice**, not join order, so a host who stays muted for the
first five minutes is not permanently "Speaker 1".

## Key entry points

| What | Where |
|---|---|
| FastAPI app + all endpoints | `utils/server_app.py` |
| Sarvam streaming client | `utils/sarvam_streaming.py` |
| Debounced LLM extractor | `utils/llm_extractor.py` |
| Speaker attribution from voice activity | `utils/speaker_registry.py` |
| Browser audio capture + speaker taps | `utils/meet_audio_capture.js` |
| Live transcript state | `utils/transcript_manager.py` |
| Browser audio capture | `utils/meet_audio_capture.js` |
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