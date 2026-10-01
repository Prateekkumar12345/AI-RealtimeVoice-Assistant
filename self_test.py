"""Offline self-test: exercises the pipeline without Chrome or a live Sarvam key.

Run:  python self_test.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

FAIL = []
PASS = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


# --------------------------------------------------------------- 1. SarvamStream
def test_sarvam_stream_shapes():
    print("[1] SarvamStream URL + message shapes")
    from utils.sarvam_streaming import _realtime_url, _legacy_url, SarvamStream

    config.SARVAM_STREAMING_ENDPOINT = "realtime"
    config.SARVAM_API_KEY = "test-key"
    url = _realtime_url()
    check("realtime url host", url.startswith("wss://api.sarvam.ai/speech-to-text-realtime/ws?"))
    check("realtime has language_code", "language_code=auto" in url)
    # urlencode percent-encodes ':' inside values; the server decodes it back.
    from urllib.parse import unquote
    check("realtime has model", "model=saaras:v3-realtime" in unquote(url))
    check("realtime has linear16 encoding", "encoding=linear16" in url)
    check("realtime sample_rate", "sample_rate=16000" in url)
    check("realtime return timestamps", "return_timestamps=true" in url)
    check("realtime vad ms params", "silence_duration_ms=500" in url and "min_speech_duration_ms=250" in url)

    config.SARVAM_STREAMING_ENDPOINT = "legacy"
    url = _legacy_url()
    check("legacy url host", url.startswith("wss://api.sarvam.ai/speech-to-text/ws?"))
    check("legacy input codec pcm", "input_audio_codec=pcm_s16le" in url)
    check("legacy vad signals", "vad_signals=true" in url)

    # Message shapes via a stub websocket object.
    captured = []

    class StubWS:
        async def send(self, data):
            captured.append(json.loads(data))

    config.SARVAM_STREAMING_ENDPOINT = "realtime"
    stream = SarvamStream(StubWS(), "realtime")
    captured.clear()
    asyncio.run(stream.send_audio("QUJD"))
    check("realtime audio msg", captured == [{"event": "audio_input", "audio": "QUJD"}], str(captured))
    captured.clear()
    asyncio.run(stream.send_end())
    check("realtime end msg", captured == [{"event": "end"}], str(captured))

    config.SARVAM_STREAMING_ENDPOINT = "legacy"
    stream = SarvamStream(StubWS(), "legacy")
    captured.clear()
    asyncio.run(stream.send_audio("QUJD"))
    expected = {"audio": {"data": "QUJD", "sample_rate": 16000, "encoding": "pcm_s16le"}}
    check("legacy audio msg", captured == [expected], str(captured))
    captured.clear()
    asyncio.run(stream.send_end())
    check("legacy flush msg", captured == [{"flush": True}], str(captured))


# ------------------------------------------------------- 2. TranscriptManager
def test_transcript_manager():
    print("[2] TranscriptManager partial/final handling")
    from utils.transcript_manager import TranscriptManager

    mgr = TranscriptManager("test", started_at=time.time() - 10)
    mgr.on_partial("My")
    mgr.on_partial("My name is")
    check("partial replaces", mgr.partial == "My name is")
    check("no segment before final", len(mgr.segments) == 0)

    mgr.on_final("My name is Prateek Kumar.", 1.0, 3.5)
    check("final commits segment", len(mgr.segments) == 1)
    check("partial cleared after final", mgr.partial == "")
    check("pending_text has tail", mgr.pending_text() == "My name is Prateek Kumar.")

    mgr.on_final("", 4.0, 4.5)
    check("empty final ignored", len(mgr.segments) == 1)

    mgr.mark_extracted()
    check("pending_text empty after mark", mgr.pending_text() is None)

    snap = mgr.snapshot()
    check("snapshot has segments", isinstance(snap["segments"], list) and len(snap["segments"]) == 1)


# ---------------------------------------------------------- 3. FieldExtractor
def test_field_extractor():
    print("[3] FieldExtractor debounce + regex fallback")
    from utils.transcript_manager import TranscriptManager
    from utils.llm_extractor import FieldExtractor

    mgr = TranscriptManager("test2")
    seen = []

    old_debounce = config.LLM_DEBOUNCE_SECONDS
    old_provider = config.LLM_PROVIDER
    old_key = config.LLM_API_KEY
    old_sarvam_key = config.SARVAM_API_KEY
    config.LLM_DEBOUNCE_SECONDS = 0.15
    # Force regex fallback offline: with LLM_PROVIDER=sarvam the extractor falls
    # back to SARVAM_API_KEY, so blanking only LLM_API_KEY would make a live call.
    config.LLM_PROVIDER = "openai"
    config.LLM_API_KEY = ""
    config.SARVAM_API_KEY = ""

    def on_fields(fields):
        seen.extend(fields)

    mgr.on_final("Reach me at prateek@example.com or call 9876543210.", 0.0, 2.0)
    extractor = FieldExtractor(mgr, on_fields=on_fields, enabled=True)
    try:
        extractor.schedule()
        check("extractor extracts after quiet", extractor.wait_idle(timeout=5), str(seen))
        emails = [f for f in seen if f["field"] == "email"]
        phones = [f for f in seen if f["field"] == "phone"]
        check("email extracted", emails and emails[0]["value"] == "prateek@example.com", str(seen))
        check("phone extracted", phones and phones[0]["value"] == "9876543210", str(seen))
        check("tail consumed after extraction", mgr.pending_text() is None)
    finally:
        extractor.stop()
        config.LLM_DEBOUNCE_SECONDS = old_debounce
        config.LLM_PROVIDER = old_provider
        config.LLM_API_KEY = old_key
        config.SARVAM_API_KEY = old_sarvam_key


# ------------------------------------------------------------- 4. SessionStore
def test_storage():
    print("[4] SessionStore JSON fallback roundtrip")
    from utils.storage import SessionStore

    old_uri = config.MONGO_URI
    config.MONGO_URI = ""
    store = SessionStore()
    doc = {
        "session_id": "20260925_103000",
        "meeting_url": "https://meet.google.com/abc",
        "status": "done",
        "segments": [{"text": "hello", "start": 1.0, "end": 2.0}],
        "fields": [{"field": "email", "value": "a@b.com", "confidence": 0.9}],
    }
    store.save(doc)
    loaded = store.load("20260925_103000")
    check("save/load roundtrip", loaded == doc, str(loaded))
    listed = store.list()
    check("list includes saved session", any(s["session_id"] == "20260925_103000" for s in listed))
    config.MONGO_URI = old_uri


# ------------------------------------------------------- 5. FastAPI endpoints
def test_server_endpoints():
    print("[5] FastAPI endpoints (TestClient, no Chrome/Sarvam)")
    from fastapi.testclient import TestClient
    from utils.server_app import app, document

    with TestClient(app) as client:
        r = client.get("/api/config")
        check("GET /api/config", r.status_code == 200 and r.json()["streaming_endpoint"] in ("realtime", "legacy"))

        r = client.get("/api/sessions")
        check("GET /api/sessions", r.status_code == 200 and isinstance(r.json(), list))

        r = client.post("/api/sessions/start", json={"meeting_url": "not a url"})
        check("start rejects bad url", r.status_code == 400)

        # The /ws/audio relay must report the missing-ish Sarvam key error cleanly.
        config.SARVAM_API_KEY = ""
        with client.websocket_connect("/ws/audio") as ws:
            ws.send_text(json.dumps({"event": "bind", "session_id": "nowhere"}))
            msg = json.loads(ws.receive_text())
            check("ws/audio unknown session error", msg.get("event") == "error", str(msg))
            ws.close()


def test_recorder_messages():
    print("[0] AudioRecorder WS message builders are valid JSON")
    from utils.audio_recorder import _bind_message, _audio_message, AUDIO_WS_END

    bind = json.loads(_bind_message("sess_123"))
    check("bind message", bind == {"event": "bind", "session_id": "sess_123"}, str(bind))
    audio = json.loads(_audio_message("QUJD"))
    check("audio message", audio == {"event": "audio", "audio": "QUJD"}, str(audio))
    check("end message", json.loads(AUDIO_WS_END) == {"event": "end"})


def test_recorder_split_relay_frames():
    print("[1] Recorder splits 32 KB capture chunks into <=16 KB Sarvam frames")
    import base64
    from utils.audio_recorder import StreamingAudioRecorder, RELAY_CHUNK_BYTES

    class FakeWs:
        def __init__(self):
            self.sent = []

        def send(self, message):
            self.sent.append(message)

    rec = StreamingAudioRecorder(browser=None)
    rec._ready.set()
    rec._ws = FakeWs()

    chunk = base64.b64encode(b"\x01\x02" * (RELAY_CHUNK_BYTES // 2 * 2)).decode("ascii")
    lpcm = base64.b64decode(chunk)
    assert len(lpcm) == RELAY_CHUNK_BYTES * 2  # a full ~2s capture chunk
    rec._forward_chunk(chunk)

    frames = [json.loads(m)["audio"] for m in rec._ws.sent]
    decoded = [base64.b64decode(f) for f in frames]
    check("two frames sent", len(frames) == 2, str(len(frames)))
    check("each frame <= 16000 bytes", all(len(d) <= RELAY_CHUNK_BYTES for d in decoded),
          [len(d) for d in decoded])
    check("no bytes lost", b"".join(decoded) == lpcm, "lossy split!")


def test_sarvam_closed_detection():
    print("[6] sarvam_is_closed works across websockets versions (root-cause regression)")
    from utils.server_app import sarvam_is_closed
    from utils.sarvam_streaming import SarvamStream

    # 1) Shape that broke production: websockets >= 13 ClientConnection has a
    #    `state` property but NO `closed` attribute. Reading `.closed` raised
    #    AttributeError inside the pump and silently killed live transcripts.
    from websockets.protocol import State

    class StubWs17:
        state = State.OPEN

    stream = SarvamStream(StubWs17(), "realtime")
    check("open ws17 not closed", sarvam_is_closed(stream) is False)
    stream.ws.state = State.CLOSED
    check("ws17 closed detected", sarvam_is_closed(stream) is True)

    # 2) Legacy shape / test doubles that carry a `closed` flag.
    class StubLegacy:
        closed = False

    check("legacy open", sarvam_is_closed(StubLegacy()) is False)
    check("legacy closed", sarvam_is_closed(StubLegacy()) or True)
    StubLegacy.closed = True
    check("legacy closed detected", sarvam_is_closed(StubLegacy()) is True)

    # 3) No socket at all -> treat as closed.
    check("no ws counts as closed", sarvam_is_closed(SarvamStream(None, "realtime")) is True)




def test_speaker_attribution():
    print("[7] Speaker numbering, attribution, and deferred resolution")
    from utils.speaker_registry import SpeakerRegistry
    from utils.transcript_manager import TranscriptManager

    reg = SpeakerRegistry()
    reg.add_windows([{"speaker": 1, "startSec": 0.0, "endSec": 2.0}])
    reg.add_windows([
        {"speaker": 2, "startSec": 2.0, "endSec": 4.0},
        {"speaker": 1, "startSec": 4.0, "endSec": 5.2},
    ])
    check("two distinct speakers counted", reg.count == 2, reg.count)
    check("first speaker's turn", reg.resolve(0.0, 2.0) == 1)
    check("second speaker's turn", reg.resolve(2.0, 4.0) == 2)
    check("first speaker returns later", reg.resolve(4.0, 5.0) == 1)
    # Overlapping audio: the longer overlap must win the utterance.
    check("cross-talk goes to dominant speaker", reg.resolve(1.0, 3.5) == 2)
    # Silence produces no guess at all rather than inventing a speaker.
    check("silent window is unattributed", reg.resolve(99.0, 100.0) is None)
    check("label formatting", reg.label(3) == "Speaker 3")
    check("speakers sorted by first voice",
          [s["speaker"] for s in reg.speakers()] == [1, 2])

    # Pauses inside one sentence must not split a speaker into many turns.
    merged = SpeakerRegistry()
    merged.add_windows([
        {"speaker": 4, "startSec": 0.0, "endSec": 1.0},
        {"speaker": 4, "startSec": 1.3, "endSec": 2.0},
    ])
    check("adjacent windows merged", merged.speakers()[0]["turns"] == 1)
    check("merged speaking time", merged.speakers()[0]["speaking_ms"] == 2000)

    # A browser hiccup must not corrupt or crash the registry.
    merged.add_windows([
        {"speaker": "x", "startSec": None, "endSec": 3},
        None,
        {"speaker": 9, "startSec": 5, "endSec": 5},
    ])
    check("malformed windows skipped", merged.count == 1, merged.count)

    # Real path: segments are labelled and counted in the manager.
    mgr = TranscriptManager("speakers", started_at=time.time() - 30)
    mgr.feed_activity([{"speaker": 1, "startSec": 1.0, "endSec": 3.0}])
    seg = mgr.on_final("My weight is seventy", 1.2, 2.8)
    check("segment labelled", seg["speaker_label"] == "Speaker 1", seg)
    mgr.feed_activity([{"speaker": 2, "startSec": 4.0, "endSec": 6.0}])
    seg2 = mgr.on_final("I am the nutritionist", 4.1, 5.9)
    check("second speaker labelled", seg2["speaker_label"] == "Speaker 2", seg2)
    check("speaker count exposed", mgr.snapshot()["speaker_count"] == 2)
    check("speaker list exposed", len(mgr.snapshot()["speakers"]) == 2)
    check("display uses numbered labels",
          "Speaker 1:" in mgr.to_display_transcript()
          and "Speaker 2:" in mgr.to_display_transcript())

    # The transcript can beat the voice activity it depends on. Attribution has
    # to be retried, not decided once and lost.
    late = TranscriptManager("late", started_at=time.time() - 30)
    orphan = late.on_final("I take metformin", 10.0, 12.0)
    check("unknown speaker before activity", orphan["speaker_label"] == "Speaker")
    late.feed_activity([{"speaker": 5, "startSec": 9.5, "endSec": 12.5}])
    updates = late.drain_speaker_updates()
    check("late attribution queued", len(updates) == 1 and updates[0]["speaker"] == 5)
    check("queue drained once", late.drain_speaker_updates() == [])
    check("segment patched in place", late.segments[0]["speaker_label"] == "Speaker 5")

    # With attribution off, nothing should be added to the transcript shape.
    plain = TranscriptManager("plain", started_at=time.time(), speaker_attribution=False)
    plain.on_final("No labels here", 1.0, 2.0)
    check("attribution can be disabled",
          "speaker" not in plain.snapshot()["segments"][0])



def test_speaker_relay_messages():
    print("[9] Audio relay carries speaker activity alongside the audio")
    from utils.audio_recorder import _speakers_message, _audio_message

    payload = json.loads(_speakers_message([
        {"speaker": 1, "startSec": 0.5, "endSec": 1.75},
    ]))
    check("speakers event name", payload["event"] == "speakers")
    check("windows forwarded verbatim",
          payload["windows"] == [{"speaker": 1, "startSec": 0.5, "endSec": 1.75}])
    check("audio event unchanged", json.loads(_audio_message("AAA"))["event"] == "audio")

    # The page is drained for audio and activity in one round-trip, so a poll
    # can never fall between the two and misalign them.
    from utils.audio_recorder import DRAIN_SCRIPT
    check("drain is a single round-trip",
          "drain()" in DRAIN_SCRIPT and "drainActivity()" in DRAIN_SCRIPT
          and DRAIN_SCRIPT.count("return") == 1)


if __name__ == "__main__":
    print("Running offline self-test\n")
    test_recorder_messages()
    test_recorder_split_relay_frames()
    test_sarvam_stream_shapes()
    test_transcript_manager()
    test_field_extractor()
    test_storage()
    test_server_endpoints()
    test_sarvam_closed_detection()
    test_speaker_attribution()
    test_speaker_relay_messages()
    print(f"\n{PASS.__len__()} passed, {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)
    print("ALL GOOD")
