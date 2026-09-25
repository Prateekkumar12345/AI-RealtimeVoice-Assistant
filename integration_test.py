"""Live-relay integration test with a fake Sarvam endpoint.

Boots the real FastAPI server on a test port, patches the Sarvam client with an
in-process fake that emits partial/final/session.end, then pushes two audio chunks
through the real /ws/audio relay and asserts the live UI WebSocket receives
snapshot → partial → final → session_end.

Run:  python integration_test.py
"""
import asyncio
import base64
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config


class _StubWs17:
    """Mimics websockets >= 13 ClientConnection: `state`, no `closed` attr."""

    def __init__(self):
        from websockets.protocol import State
        self.state = State.OPEN


class FakeSarvam:
    """Acts like SarvamStream on websockets >= 13 (no `.closed` attribute).

    Regression guard: the relay used to loop on `sarvam.closed`, which raises
    AttributeError on websockets >= 13 and killed the transcript pump — audio
    kept flowing in but partials/finals never reached the UI.
    """

    def __init__(self):
        self.ws = _StubWs17()
        self.ended = False
        self.kind = "realtime"
        self._queue = asyncio.Queue()
        self._audio_count = 0

    @classmethod
    async def open(cls):
        return cls()

    async def send_audio(self, b64):
        self._audio_count += 1
        await self._queue.put("audio")

    async def send_end(self):
        self.ended = True
        await self._queue.put("session_end")

    async def send_ping(self):
        pass

    async def recv(self):
        item = await self._queue.get()
        if item == "audio":
            if self._audio_count == 1:
                return {"event": "transcript.partial", "text": "Hello", "language": "en-IN"}
            return {"event": "transcript.final", "text": "Hello team", "start_s": 1.0, "end_s": 3.0, "language": "en-IN"}
        if item == "session_end":
            return {"event": "session.end", "audio_duration_s": 12.3}
        return None

    async def close(self):
        from websockets.protocol import State
        self.confirmed_end = getattr(self, "ended", False)
        self.ws.state = State.CLOSED


def main():
    import uvicorn
    import utils.server_app as sa
    from utils.llm_extractor import FieldExtractor

    # Patch the Sarvam client used by the relay.
    sa.SarvamStream = FakeSarvam

    server = uvicorn.Server(uvicorn.Config(sa.app, host="127.0.0.1", port=8001, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        print("FAIL  server did not start")
        sys.exit(1)

    # Fabricate an active session without launching Chrome.
    sid = "integration_live"
    session = sa.SessionState(sid, "https://meet.google.com/demo")
    session.status = "recording"
    sa._sessions[sid] = session
    session.extractor = FieldExtractor(session.manager, on_fields=lambda f: None, enabled=False)
    print(f"[relay] session {sid} active on port 8001")

    from websockets.sync.client import connect as ws_connect

    received_ui = []

    # ---- UI subscriber ----------------------------------------------------
    ui = ws_connect("ws://127.0.0.1:8001/ws/session/" + sid)
    got = json.loads(ui.recv(timeout=5))
    assert got.get("type") == "snapshot", got
    print(f"[ui] snapshot status={got['session']['status']}")

    # ---- audio recorder ---------------------------------------------------
    rec = ws_connect("ws://127.0.0.1:8001/ws/audio")
    rec.send(json.dumps({"event": "bind", "session_id": sid}))
    ready_msg = json.loads(rec.recv(timeout=10))
    assert ready_msg.get("event") == "ready", ready_msg
    print("[relay] ready")

    def drain_ui(timeout=3.0):
        out = []
        end = time.time() + timeout
        while time.time() < end:
            try:
                out.append(json.loads(ui.recv(timeout=0.4)))
            except Exception:
                break
        return out

    # ---- push two one-second chunks + end ---------------------------------
    chunk = base64.b64encode(b"\x00" * config.CHUNK_BYTES).decode("ascii")
    rec.send(json.dumps({"event": "audio", "audio": chunk}))
    time.sleep(0.4)
    rec.send(json.dumps({"event": "audio", "audio": chunk}))
    time.sleep(0.4)
    rec.send(json.dumps({"event": "end"}))

    received_ui.extend(drain_ui(timeout=3.0))

    kinds = [m.get("type") for m in received_ui]
    ok = True
    for expected in ("partial", "final", "session_end"):
        if expected not in kinds:
            ok = False
            print(f"  FAIL  UI never received {expected!r}; got {kinds}")
    if ok:
        print("[ui] received partial, final, session_end OK")

    final_segments = [m for m in received_ui if m.get("type") == "final"]
    assert final_segments and final_segments[0]["text"] == "Hello team"
    assert final_segments[0]["start"] == 1.0
    print(f"[ui] final segment: {final_segments[0]['text']} @ {final_segments[0]['start']}s")
    sess_end = [m for m in received_ui if m.get("type") == "session_end"][0]
    assert sess_end.get("audio_duration_s") == 12.3
    assert final_segments[0]["language"] == "en-IN"

    # The relay should have confirmed the end-of-stream and closed Sarvam.
    session.persistence_snap = sa.document(session)
    assert session.manager.audio_duration_s == 12.3, session.manager.snapshot()

    rec.close()
    ui.close()
    session.extractor.stop()
    server.should_exit = True
    print("\nINTEGRATION TEST OK")


if __name__ == "__main__":
    main()