"""FastAPI server: REST control-plane + audio relay + live session broadcast.

Endpoints
---------
POST /api/sessions/start          {meeting_url} -> start a live bot session
POST /api/sessions/{sid}/stop     stop recording, flush LLM extraction, finalize
GET  /api/sessions/{sid}          current session document
GET  /api/sessions                list past sessions
GET  /api/config                  sanitized runtime configuration
WS   /ws/audio                    audio relay: bot chunk -> Sarvam streaming STT
WS   /ws/session/{sid}            live transcript/fields/status events for the UI
GET  /                            serves the React build (frontend/dist) when present
"""
import asyncio
import json
import os
import threading
import time
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from websockets.exceptions import ConnectionClosed

import config
from utils import chrome_login, session_vault
from utils.meet_bot import GoogleMeetBot
from utils.transcript_manager import TranscriptManager
from utils.llm_extractor import FieldExtractor
from utils.storage import SessionStore
from utils.sarvam_streaming import SarvamStream, SarvamStreamError

app = FastAPI(title="Voicebot live server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STORE = SessionStore()

# Registrar of active sessions, keyed by session_id.
_active_lock = threading.Lock()
_sessions = {}  # sid -> SessionState
LOOP = None


class SessionState:
    def __init__(self, sid, meeting_url):
        self.session_id = sid
        self.meeting_url = meeting_url
        self.started_at = time.time()
        self.ended_at = None
        self.status = "joining"  # joining | recording | stopping | done | failed
        self.error = None
        self.manager = TranscriptManager(sid, self.started_at)
        self.extractor = None
        self.bot = None
        self.wav_file = None
        self.fields = {}  # (field, value) -> {field, value, confidence, count}
        self.fields_list = []
        self.subscribers = []
        self.sarvam = None
        self.sarvam_state = "none"  # none | connecting | ready | error | closed
        self.sarvam_error = None
        self.conn_started_at = time.time()
        self.last_speech_start = None
        self.audio_duration_s = None
        # No-audio detection: the bot only records remote WebRTC audio tracks
        # from other participants, so if everyone stays muted there is genuinely
        # nothing to capture. We can't unmute anyone, but we can flag it fast
        # instead of failing silently. See _handle_capture_monitor().
        self.last_audio_seen_at = None
        self.audio_warned = False
        # True while a bot's browser teardown (leave_meeting -> browser.quit())
        # is still running in a background thread. A new session must not be
        # allowed to start until this finishes, or its own Chrome-process
        # cleanup can kill the still-quitting bot's chromedriver out from
        # under it ("no such window: target window already closed").
        self.cleanup_pending = False

    async def broadcast(self, message):
        for ws in list(self.subscribers):
            try:
                await ws.send_json(message)
            except Exception:
                self.subscribers.remove(ws)

    def add_field(self, fields):
        for item in fields:
            key = (item["field"], item["value"])
            existing = self.fields.get(key)
            if existing:
                existing["count"] = existing.get("count", 1) + 1
                continue
            self.fields[key] = dict(item, count=1)
        self.fields_list = [dict(v, field=k[0], value=k[1]) for k, v in self.fields.items()]


def document(session, include_partial=True):
    snap = session.manager.snapshot()
    doc = {
        "session_id": session.session_id,
        "meeting_url": session.meeting_url,
        "status": session.status,
        "error": session.error,
        "sarvam_state": session.sarvam_state,
        "sarvam_error": session.sarvam_error,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "partial": snap["partial"] if include_partial else "",
        "language": snap["language"],
        "segments": snap["segments"],
        "fields": session.fields_list,
        "wav_file": session.wav_file,
        "audio_duration_s": session.audio_duration_s,
        # Which step of the join the bot is on (launching -> loading -> prejoin ->
        # joining -> waiting_for_admission -> in_meeting -> recording), so a stuck
        # join can be told apart from a Meet refusal.
        "bot_state": session.bot.state if session.bot else None,
    }
    return doc


def _persist(session):
    try:
        STORE.save(document(session))
    except Exception as error:
        print(f"Persist failed for {session.session_id}: {error}")


def _broadcast(session, message):
    if LOOP is None or session is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(session.broadcast(message), LOOP)
    except Exception as error:
        print(f"Async broadcast failed: {error}")


class JoinRequest(BaseModel):
    meeting_url: str


# ------------------------------------------------------------------ REST
def _llm_provider_configured():
    """True when any LLM extraction path can authenticate.

    The Sarvam provider reuses the STT subscription key, so the UI must not
    claim 'regex-fallback' just because LLM_API_KEY itself is empty.
    """
    if config.LLM_PROVIDER == "sarvam":
        return bool(config.SARVAM_API_KEY or config.LLM_API_KEY)
    return bool(config.LLM_API_KEY)


@app.get("/api/config")
def get_config():
    return {
        "streaming_endpoint": config.SARVAM_STREAMING_ENDPOINT,
        "streaming_model": config.SARVAM_STREAMING_MODEL,
        "stream_type": config.SARVAM_STREAM_TYPE,
        "mode": config.SARVAM_STREAMING_MODE,
        "language_code": config.SARVAM_LANGUAGE_CODE,
        "llm_enabled": config.LLM_EXTRACTION_ENABLED,
        "llm_provider": config.LLM_PROVIDER,
        "llm_model": config.LLM_MODEL,
        "llm_provider_configured": _llm_provider_configured(),
        "mongo_configured": bool(config.MONGO_URI),
        # Never includes the password: only whether one is set, plus what the
        # session vault currently holds (email, cookie count, expiry).
        "auto_login": config.AUTO_LOGIN,
        "google_credentials_configured": chrome_login.credentials_configured(),
        "session_vault": session_vault.status(),
    }


@app.post("/api/sessions/start")
def start_session(payload: JoinRequest):
    if not (payload.meeting_url or "").startswith(("http://", "https://")):
        raise HTTPException(400, "meeting_url must be an http(s) URL")

    with _active_lock:
        for session in _sessions.values():
            if session.status in ("joining", "recording", "stopping") or session.cleanup_pending:
                raise HTTPException(400, "A session is already in progress")
        sid = datetime.now().strftime("%Y%m%d_%H%M%S")
        session = SessionState(sid, payload.meeting_url)
        _sessions[sid] = session
        session.status = "joining"
        session.extractor = FieldExtractor(
            session.manager,
            on_fields=lambda fields: _on_fields(session, fields),
        )
        _persist(session)

    threading.Thread(target=_join_worker, args=(session,), daemon=True).start()
    return {"session_id": sid, "ws_url": f"/ws/session/{sid}"}


@app.post("/api/sessions/{sid}/stop")
def stop_session(sid: str):
    session = _sessions.get(sid)
    if not session:
        raise HTTPException(404, "Session not found")
    if session.status != "recording":
        raise HTTPException(400, f"Session is not recording (status={session.status})")
    session.status = "stopping"
    _persist(session)
    _broadcast(session, {"type": "status", "status": "stopping"})
    threading.Thread(target=_stop_worker, args=(session,), daemon=True).start()
    return {"accepted": True}


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    session = _sessions.get(sid)
    if session:
        return document(session)
    stored = STORE.load(sid)
    if not stored:
        raise HTTPException(404, "Session not found")
    return stored


@app.get("/api/sessions")
def list_sessions():
    return STORE.list()


# ------------------------------------------------------------------ workers
def _join_worker(session):
    bot = GoogleMeetBot()
    session.bot = bot
    try:
        if not bot.join_meeting(session.meeting_url, session_id=session.session_id):
            session.status = "failed"
            session.error = bot.last_error or "Failed to join meeting"
            _persist(session)
            _broadcast(session, {"type": "status", "status": "failed", "error": session.error})
            _safe_leave(session, bot)
            return

        started = bot.start_recording(session.session_id)
        if not started:
            session.status = "failed"
            session.error = bot.last_error or "Failed to start recording"
            _persist(session)
            _broadcast(session, {"type": "status", "status": "failed", "error": session.error})
            _safe_leave(session, bot)
            return

        session.status = "recording"
        _persist(session)
        _broadcast(session, {"type": "status", "status": "recording"})
    except Exception as error:
        session.status = "failed"
        session.error = str(error)
        _persist(session)
        _broadcast(session, {"type": "status", "status": "failed", "error": session.error})
        _safe_leave(session, bot)


def _safe_leave(session, bot):
    """Always tear the browser + chromedriver down, even on a failed join.
    Leaving this unclosed is what causes later sessions to time out talking
    to chromedriver (orphaned Chrome/chromedriver processes pile up).

    cleanup_pending stays True for the whole teardown so a new session
    can't start (and run close_stale_automation_chrome()) while this bot's
    browser.quit() is still in flight -- that race is what produces
    'no such window: target window already closed' from a second bot's
    startup killing this one's chromedriver mid-shutdown.
    """
    session.cleanup_pending = True
    try:
        bot.leave_meeting()
    except Exception as error:
        print(f"Cleanup after failed join did not fully succeed: {error}")
    finally:
        session.cleanup_pending = False


def _stop_worker(session):
    try:
        if session.bot:
            session.wav_file = session.bot.stop_recording()
        session.extractor.flush_now()
        session.extractor.wait_idle(timeout=config.LLM_TIMEOUT + 5)
        session.status = "done"
        session.ended_at = time.time()
    except Exception as error:
        session.status = "failed"
        session.error = str(error)
    finally:
        # Without this, the bot's Chrome window (and its chromedriver process)
        # is left running forever after every single session, success or not.
        if session.bot:
            _safe_leave(session, session.bot)
    _persist(session)
    _broadcast(session, {"type": "done", "session": document(session, include_partial=False)})


def _on_fields(session, fields):
    session.add_field(fields)
    _persist(session)
    _broadcast(session, {"type": "fields", "fields": fields})


# ------------------------------------------------------------------ streaming
async def _handle_sarvam_message(session, msg):
    """Normalise realtime / legacy Sarvam events into manager updates + UI events."""
    if not isinstance(msg, dict):
        return False

    # --- realtime endpoint -------------------------------------------------
    if "event" in msg:
        event = msg["event"]
        if event == "transcript.partial":
            text = msg.get("text") or ""
            session.manager.on_partial(text, msg.get("language"))
            await session.broadcast({
                "type": "partial",
                "text": text,
                "language": msg.get("language"),
            })
        elif event == "transcript.final":
            start = _as_seconds(msg.get("start_s"), session)
            end = _as_seconds(msg.get("end_s"), session, fallback_now=True)
            language = msg.get("language")
            segment = session.manager.on_final(msg.get("text"), start, end, language)
            if segment:
                _persist(session)
                await session.broadcast({"type": "final", **segment})
                session.extractor.schedule()
            return False
        elif event == "vad.speech_start":
            session.last_speech_start = time.time()
            await session.broadcast({"type": "vad", "state": "speech_start"})
        elif event == "vad.speech_end":
            await session.broadcast({"type": "vad", "state": "speech_end"})
        elif event == "session.begin":
            session.conn_started_at = time.time()
        elif event == "session.end":
            session.audio_duration_s = msg.get("audio_duration_s")
            session.manager.on_session_end(session.audio_duration_s)
            _persist(session)
            await session.broadcast({"type": "session_end", "audio_duration_s": session.audio_duration_s})
            return True  # caller should close
        elif event == "error":
            fatal = bool(msg.get("is_fatal"))
            session.manager.on_error(msg.get("message"), fatal)
            _persist(session)
            await session.broadcast({
                "type": "stt_error",
                "message": msg.get("message"),
                "fatal": fatal,
            })
            return fatal
        return False

    # --- legacy endpoint ---------------------------------------------------
    msg_type = msg.get("type")
    data = msg.get("data") or {}
    if msg_type == "data":
        text = data.get("transcript") or ""
        if text:
            end = time.time() - session.conn_started_at
            segment = session.manager.on_final(text, None, round(end, 2), None)
            if segment:
                _persist(session)
                await session.broadcast({"type": "final", **segment})
                session.extractor.schedule()
    elif msg_type == "events":
        signal = data.get("signal_type")
        if signal == "START_SPEECH":
            session.last_speech_start = time.time()
            await session.broadcast({"type": "vad", "state": "speech_start"})
        elif signal == "END_SPEECH":
            await session.broadcast({"type": "vad", "state": "speech_end"})
    elif msg_type == "error":
        message = data.get("message") if isinstance(data, dict) else str(data)
        session.manager.on_error(message, False)
        _persist(session)
        await session.broadcast({"type": "stt_error", "message": message, "fatal": False})
        return True
    elif msg_type == "end":
        return True
    return False


async def _handle_capture_monitor(session, status):
    """Watch the page-side capture stats (tracksSeen/peak) and tell the UI
    when no participant audio has been seen for a while, instead of quietly
    writing a silent WAV. This can only detect the problem — the bot has no
    way to unmute another participant's microphone for them."""
    tracks_seen = status.get("tracksSeen") or 0
    peak = status.get("peak") or 0
    now = time.time()

    heard_audio = tracks_seen > 0 or peak > 0.005
    if heard_audio:
        session.last_audio_seen_at = now
        if session.audio_warned:
            session.audio_warned = False
            await session.broadcast({
                "type": "audio_warning",
                "active": False,
            })
        return

    since = now - (session.last_audio_seen_at or session.conn_started_at)
    if since >= config.NO_AUDIO_WARNING_SECONDS and not session.audio_warned:
        session.audio_warned = True
        await session.broadcast({
            "type": "audio_warning",
            "active": True,
            "message": (
                "No audio detected from the meeting yet. The bot only records "
                "other participants' microphones (it can't unmute them for "
                "you) — ask everyone in the call to check their mic is on."
            ),
            "seconds": round(since),
        })


def _as_seconds(value, session, fallback_now=False):
    if value in (None, ""):
        if fallback_now and session.last_speech_start:
            return round(time.time() - session.last_speech_start, 2)
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    await ws.accept()
    session = None
    sarvam = None
    try:
        first = json.loads(await ws.receive_text())
        if first.get("event") != "bind" or not first.get("session_id"):
            await ws.send_text(json.dumps({"event": "error", "message": "bind required"}))
            await ws.close(code=1008)
            return
        session = _sessions.get(first["session_id"])
        if session is None:
            await ws.send_text(json.dumps({"event": "error", "message": "unknown session"}))
            await ws.close(code=1008)
            return

        try:
            sarvam = await SarvamStream.open()
        except SarvamStreamError as error:
            await ws.send_text(json.dumps({"event": "error", "message": str(error)}))
            session.manager.on_error(str(error), fatal=True)
            session.sarvam_state = "error"
            session.sarvam_error = str(error)
            _persist(session)
            # Without this broadcast the browser never learns the STT socket
            # failed to open — it just sees WAV recording continue forever
            # with an empty transcript pane and no error anywhere.
            await session.broadcast({
                "type": "stt_error",
                "message": str(error),
                "fatal": True,
            })
            await session.broadcast({"type": "debug", "stt": "error",
                                     "sarvam_error": str(error), "capture": {}})
            await ws.close(code=1011)
            return

        session.sarvam = sarvam
        session.sarvam_state = "ready"
        session.sarvam_error = None
        # Real-time timestamps start when the socket is established.
        session.conn_started_at = time.time()
        await ws.send_text(json.dumps({"event": "ready"}))
        await session.broadcast({"type": "debug", "stt": "ready",
                                 "capture": {}, "sarvam_error": None})

        async def client_pump():
            async for raw in ws.iter_text():
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = msg.get("event")
                if event == "audio":
                    await sarvam.send_audio(msg["audio"])
                elif event == "end":
                    await sarvam.send_end()
                    break
                elif event == "ping":
                    pass
                elif event == "monitor":
                    status = msg.get("status") or {}
                    await session.broadcast({
                        "type": "debug",
                        "stt": session.sarvam_state,
                        "sarvam_error": session.sarvam_error,
                        "capture": status,
                        "wav_bytes": status.get("wav_bytes"),
                    })
                    await _handle_capture_monitor(session, status)

        async def ping_loop():
            # Sarvam closes the socket with code 1008 if nothing is sent for a
            # while (e.g. silence in the meeting), which would kill live
            # transcription mid-session. An app-level ping keeps it open.
            try:
                while not sarvam_is_closed(sarvam):
                    await asyncio.sleep(config.SARVAM_PING_INTERVAL)
                    if sarvam_is_closed(sarvam):
                        break
                    try:
                        await sarvam.send_ping()
                    except Exception:
                        break
            except asyncio.CancelledError:
                pass

        async def sarvam_pump():
            while not sarvam_is_closed(sarvam):
                try:
                    msg = await sarvam.recv()
                except ConnectionClosed:
                    # The server hung up (inactivity timeout, quota, fatal
                    # error...). Record and surface it instead of dying quietly:
                    # a silent pump here looks exactly like "recording works,
                    # transcript never appears" from the UI.
                    try:
                        code = sarvam.ws.close_code if sarvam.ws else None
                    except Exception:
                        code = None
                    detail = f"Sarvam socket closed by server (code={code})"
                    print(detail)
                    if code not in (1000,):
                        session.manager.on_error(detail, fatal=False)
                        _persist(session)
                        await session.broadcast({"type": "stt_error", "message": detail, "fatal": False})
                    break
                except Exception as error:
                    print(f"Sarvam receive failed: {error}")
                    break
                if msg is None:
                    continue
                try:
                    stop = await _handle_sarvam_message(session, msg)
                except Exception as error:
                    print(f"Sarvam event handling failed: {error}")
                    stop = False
                if stop:
                    await sarvam.close()
                    break

        ping_task = asyncio.create_task(ping_loop())
        try:
            await asyncio.gather(client_pump(), sarvam_pump())
        finally:
            ping_task.cancel()
            try:
                await ping_task
            except (asyncio.CancelledError, Exception):
                pass

    except WebSocketDisconnect:
        pass
    except Exception as error:
        print(f"Audio relay error: {error}")
    finally:
        if session is not None:
            session.sarvam_state = "closed"
        if sarvam is not None:
            await sarvam.close()
            if session is not None:
                await session.broadcast({"type": "debug", "stt": "closed",
                                         "sarvam_error": session.sarvam_error,
                                         "capture": {}})


def sarvam_is_closed(sarvam):
    """True when the Sarvam socket can no longer carry messages.

    websockets >= 13's ClientConnection has no `closed` attribute (only a
    `state` property and `close_code`), so the old `sarvam.closed` check raised
    AttributeError on the very first pump iteration. That killed the task that
    reads Sarvam's transcript events — audio kept flowing in, but partials and
    finals were never read, so nothing ever reached the UI.
    """
    ws = getattr(sarvam, "ws", None)
    if ws is not None:
        state = getattr(ws, "state", None)
        if state is not None:
            try:
                from websockets.protocol import State
                return state is State.CLOSED or state is State.CLOSING
            except ImportError:
                pass
    # Test doubles / older websockets versions expose `closed` instead.
    closed = getattr(sarvam, "closed", None)
    if closed is not None:
        return bool(closed)
    return ws is None


@app.websocket("/ws/session/{sid}")
async def ws_session(ws: WebSocket, sid: str):
    await ws.accept()
    session = _sessions.get(sid)
    if not session:
        await ws.send_json({"type": "error", "message": "session not found"})
        await ws.close(code=1008)
        return
    await ws.send_json({"type": "snapshot", "session": document(session)})
    session.subscribers.append(ws)
    try:
        while True:
            await ws.receive_text()  # client keep-alive / pings
    except WebSocketDisconnect:
        pass
    finally:
        if ws in session.subscribers:
            session.subscribers.remove(ws)


# ------------------------------------------------------------------ frontend
@app.get("/")
async def root():
    index_path = os.path.join(config.FRONTEND_DIST, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse({
        "service": "Voicebot live server",
        "hint": "Build the React frontend (cd frontend && npm install && npm run build) "
                "or use the API directly.",
        "endpoints": [
            "POST /api/sessions/start",
            "POST /api/sessions/{sid}/stop",
            "GET /api/sessions",
            "GET /api/sessions/{sid}",
            "WS /ws/audio",
            "WS /ws/session/{sid}",
        ],
    })


assets_dir = os.path.join(config.FRONTEND_DIST, "assets")
if os.path.isdir(assets_dir):
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.on_event("startup")
async def _capture_loop():
    global LOOP
    LOOP = asyncio.get_running_loop()