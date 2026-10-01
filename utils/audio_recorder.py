import base64
import json
import os
import struct
import threading
import time

from selenium.common.exceptions import WebDriverException

CAPTURE_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meet_audio_capture.js")

# The recorder inside meet_audio_capture.js captures 16 kHz mono 16-bit PCM.
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes per sample (PCM 16-bit)
WAV_HEADER_SIZE = 44
# Sarvam's realtime endpoint caps a single audio_input frame at 16,000 bytes
# for 16 kHz linear16 input. The page hands us 32,000-byte (1s) chunks, so each
# chunk is split into 16,000-byte (0.5s) frames before relaying.
RELAY_CHUNK_BYTES = 16000

# Blocking Selenium call used to pull one second of audio out of the page.
START_SCRIPT = """
const done = arguments[arguments.length - 1];
if (!window.__meetRec) { done({ok: false, error: 'capture script is not installed in this page'}); return; }
window.__meetRec.start().then(done, (error) => done({ok: false, error: String(error)}));
"""

# Audio and the speaker-activity windows that describe it are pulled in a single
# round-trip. Two separate execute_script() calls would let a poll interval fall
# between them, which would misalign every window from the audio it belongs to.
DRAIN_SCRIPT = """
return {
  chunks: window.__meetRec.drain(),
  activity: window.__meetRec.drainActivity ? window.__meetRec.drainActivity() : [],
};
"""

# Messages the recorder exchanges with the server's /ws/audio endpoint:
#   bind     -> {"event": "bind",     "session_id": "..."}   (first message)
#   audio    -> {"event": "audio",    "audio": "<base64>"}   (one ~1s PCM chunk)
#   speakers -> {"event": "speakers", "windows": [...]}      (voice-activity windows)
#   monitor  -> {"event": "monitor",  "status": {...}}       (capture status)
#   end      -> {"event": "end"}                             (graceful stop)
def _bind_message(session_id):
    return json.dumps({"event": "bind", "session_id": session_id})


def _audio_message(b64_pcm):
    return json.dumps({"event": "audio", "audio": b64_pcm})


def _speakers_message(windows):
    return json.dumps({"event": "speakers", "windows": windows})


AUDIO_WS_END = json.dumps({"event": "end"})


def load_capture_script():
    with open(CAPTURE_SCRIPT_PATH, "r", encoding="utf-8") as script_file:
        return script_file.read()


class StreamingAudioRecorder:
    """Dual-path recorder.

    Recording path (kept from the batch project):
        PCM chunks are appended to a 16 kHz mono WAV on disk -> the raw source of
        truth, kept even if live transcription fails.

    Live path (new):
        The exact same chunks are also forwarded over a WebSocket to the FastAPI
        server, which relays them into Sarvam's streaming STT connection.
    """

    POLL_SECONDS = 1.0
    MAX_CONSECUTIVE_FAILURES = 5
    WS_TIMEOUT = 30.0
    SESSION_END_WAIT = 10.0

    def __init__(self, browser, ws_url=None):
        self.browser = browser
        self.ws_url = ws_url
        self.is_recording = False
        self.output_path = None
        self.data_bytes = 0
        self.last_status = {}
        self._file = None
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._ws = None
        self._ws_lock = threading.RLock()  # re-entrant: _drain_chunks holds it across _forward_chunk
        self._session_id = None
        self._ready = threading.Event()
        self._error = None
        self._relay_error = False
        self._last_status_at = 0.0

    # ------------------------------------------------------------------ start
    def start_recording(self, session_id, save_directory, browser_ws_url=None):
        """session_id is used both for the WAV filename and to bind the audio WS.

        Capture startup happens inside the recording thread (see _start_capture)
        so a stalled browser call can never block the session lifecycle.
        """
        os.makedirs(save_directory, exist_ok=True)
        self._session_id = session_id
        if browser_ws_url:
            self.ws_url = browser_ws_url
        self.output_path = os.path.join(save_directory, f"{session_id}.wav")

        self._file = open(self.output_path, "wb")
        self._file.write(b"\x00" * WAV_HEADER_SIZE)  # placeholder; patched on stop
        self.data_bytes = 0
        self.drain_count = 0
        self._stop_event.clear()
        self._ready.clear()
        self.is_recording = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _start_capture(self):
        """Ask the page to start recording. Non-fatal: on failure we keep the
        session in 'recording' and surface the reason through status()."""
        try:
            self.browser.set_script_timeout(20)
            result = self.browser.execute_async_script(START_SCRIPT)
        except Exception as error:
            self.last_status = {"started": False, "error": f"capture start failed: {error}"}
            self._error = f"Audio capture could not start: {error}"
            print(self._error)
            return False

        if not (result and result.get("ok")):
            self.last_status = {"started": False, "error": (result or {}).get("error")}
            self._error = f"Audio capture could not start: {self.last_status['error']}"
            print(self._error)
            return False
        print("Audio capture started in the page")
        return True

    # ---------------------------------------------------------------- run loop
    def _run(self):
        self._start_capture()
        connected = self._connect_audio_ws()
        if connected:
            self._poll_capture_status()
        if not connected:
            # Recording path keeps working even if live transcription is down:
            # the WAV is still written so nothing is lost.
            self._error = self._error or "Audio WebSocket not available; recording WAV only"
            print(self._error)

        failures = 0
        while not self._stop_event.wait(self.POLL_SECONDS):
            wrote = self._drain_chunks()
            if wrote:
                failures = 0
            else:
                failures += 1
                if failures >= self.MAX_CONSECUTIVE_FAILURES and connected:
                    print("Audio capture lost contact with the browser; keeping what was recorded so far")
                    self.last_status = {"error": "capture lost contact with the browser"}
                    return
        self._flush_remaining()

    def _connect_audio_ws(self):
        if not self.ws_url:
            return False
        try:
            from websockets.sync.client import connect as ws_connect
            self._ws = ws_connect(self.ws_url, open_timeout=10)
            self._ws.send(_bind_message(self._session_id))
            # Wait for the server to have opened the Sarvam socket before streaming,
            # so we never drop the start of the meeting.
            deadline = time.time() + self.WS_TIMEOUT
            while time.time() < deadline:
                if self._ready.is_set():
                    return True
                if self._relay_error:
                    print("Audio relay reported an error; continuing with WAV recording only")
                    return False
                self._process_server_frame(timeout=0.1)
            print("Timed out waiting for the audio relay to become ready")
            return False
        except Exception as error:
            self._ws = None
            print(f"Audio WebSocket connect failed: {error}")
            return False

    def _process_server_frame(self, timeout=0.0):
        """Read one message pushed back from the relay (ready/error nudge)."""
        if self._ws is None:
            return
        try:
            message = self._ws.recv(timeout=timeout)
            payload = json.loads(message)
            event = payload.get("event")
            if event == "ready":
                self._ready.set()
            elif event == "error":
                print(f"Live relay reported: {payload.get('message')}")
                self._error = payload.get("message")
                self._relay_error = True
        except Exception:
            return

    def _drain_chunks(self):
        with self._lock:
            if self._file is None:
                return False
            try:
                payload = self.browser.execute_script(DRAIN_SCRIPT) or {}
            except WebDriverException:
                return False

            chunks = payload.get("chunks") or []
            # Activity goes first so the server already knows who is talking by
            # the time the transcript for that same second comes back from
            # Sarvam. Reversed, every segment would be attributed a second late.
            self._forward_activity(payload.get("activity") or [])

            for chunk in chunks:
                data = base64.b64decode(chunk)
                with self._ws_lock:
                    self._file.write(data)
                    self.data_bytes += len(data)
                    self._forward_chunk(chunk)
            if chunks:
                self._file.flush()

            if time.time() - self._last_status_at >= 2.5:
                self._last_status_at = time.time()
                self._poll_capture_status()
            self.drain_count += 1
            return True

    def _forward_activity(self, windows):
        """Hand per-speaker voice-activity windows to the server for attribution."""
        if not windows or self._ws is None:
            return
        try:
            with self._ws_lock:
                self._ws.send(_speakers_message(windows))
        except Exception:
            # Losing activity only costs speaker labels; the WAV and the
            # transcript are unaffected, so never fail recording over it.
            pass

    def _poll_capture_status(self):
        """Read window.__meetRec.status() and report it through the relay so the
        UI can show whether remote audio is actually being captured."""
        try:
            self.last_status = self.browser.execute_script("return window.__meetRec.status();") or {}
        except Exception:
            self.last_status = {"error": "status() not available in page"}
        try:
            payload = dict(self.last_status)
            payload["wav_bytes"] = self.data_bytes
            payload["drains"] = self.drain_count
            with self._ws_lock:
                self._ws.send(json.dumps({"event": "monitor", "status": payload}))
        except Exception:
            pass

    def _forward_chunk(self, b64_chunk):
        if not self._ready.is_set() or self._ws is None:
            return
        try:
            from websockets.exceptions import ConnectionClosed
            raw = base64.b64decode(b64_chunk)
            for i in range(0, max(len(raw), 1), RELAY_CHUNK_BYTES):
                piece = raw[i:i + RELAY_CHUNK_BYTES]
                with self._ws_lock:
                    self._ws.send(_audio_message(base64.b64encode(piece).decode("ascii")))
        except ConnectionClosed:
            print("Live audio relay closed; continuing with WAV recording only")
            self._ws = None
        except Exception as error:
            print(f"Live audio forward failed: {error}")
            self._ws = None

    def _flush_remaining(self):
        """Stop path: final drain, flush the WAV, and tell Sarvam the turn ended."""
        try:
            self._drain_chunks()
        except Exception:
            pass

        try:
            with self._ws_lock:
                if self._ws is not None:
                    self._ws.send(AUDIO_WS_END)
        except Exception:
            pass

        # Give the relay/Sarvam a moment to emit the final transcript before closing.
        deadline = time.time() + self.SESSION_END_WAIT
        while self._ws is not None and time.time() < deadline:
            self._process_server_frame(timeout=0.2)

    # ------------------------------------------------------------------- stop
    def stop_recording(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)

        self.is_recording = False

        try:
            self.browser.execute_script("return window.__meetRec.stop();")
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    self._drain_chunks()
                except Exception:
                    pass
                self.last_status = self.browser.execute_script("return window.__meetRec.status();") or {}
                if self.last_status.get("finished"):
                    break
                time.sleep(0.25)
            try:
                self._drain_chunks()
            except Exception:
                pass
        except WebDriverException as error:
            print(f"Could not finalize audio capture cleanly: {error}")

        if self._file:
            self._file.close()
            self._file = None

        if self.last_status.get("error"):
            print(f"Audio capture reported: {self.last_status['error']}")

        try:
            with self._ws_lock:
                if self._ws is not None:
                    self._ws.close()
                    self._ws = None
        except Exception:
            pass

        self._finalize_wav_header()

        if self.data_bytes == 0:
            return None
        return self.output_path

    def _finalize_wav_header(self):
        if not self.output_path or not os.path.exists(self.output_path):
            return
        data_len = self.data_bytes
        block_align = CHANNELS * SAMPLE_WIDTH
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 36 + data_len, b"WAVE",
            b"fmt ", 16, 1, CHANNELS, SAMPLE_RATE,
            SAMPLE_RATE * block_align, block_align, SAMPLE_WIDTH * 8,
            b"data", data_len,
        )
        try:
            with open(self.output_path, "r+b") as wav_file:
                wav_file.seek(0)
                wav_file.write(header)
        except OSError as error:
            print(f"Could not finalize WAV header: {error}")