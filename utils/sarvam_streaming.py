"""Sarvam streaming speech-to-text client (hand-rolled WebSocket).

Two endpoints are supported (select with SARVAM_STREAMING_ENDPOINT):

    realtime  (default)  /speech-to-text-realtime/ws
        saaras:v3-realtime (or saaras:v4). Emits true `transcript.partial`
        interim results while someone is speaking and `transcript.final` once an
        utterance ends (server-side VAD endpointing). Audio message format:
        {"event": "audio_input", "audio": "<base64 linear16 pcm>"}.

    legacy   /speech-to-text/ws
        saaras:v3 / saaras:v4. Only a final transcript per utterance (VAD-fenced),
        but still *live* as the meeting progresses. Raw PCM is sent directly with
        input_audio_codec=pcm_s16le.

Both use the exact audio the Meet page already produces: 16 kHz mono, 16-bit,
little-endian PCM (the WAV body). We send it as base64 so the transport is
identical whether the chunks arrive in-process or over the relay WebSocket.
"""
import json
from urllib.parse import urlencode

import config

SARVAM_WS_BASE = "wss://api.sarvam.ai"


class SarvamStreamError(Exception):
    pass


async def _connect(url):
    try:
        from websockets.asyncio.client import connect as ws_connect  # websockets >= 13
    except ImportError:
        from websockets.client import connect as ws_connect  # websockets < 13
    return await ws_connect(
        url,
        additional_headers={"api-subscription-key": config.SARVAM_API_KEY},
        ping_interval=None,  # realtime endpoint uses app-level {"event":"ping"}
        max_size=2 ** 22,
    )


def _realtime_url():
    params = {
        "language_code": config.SARVAM_REALTIME_LANGUAGE,
        "model": config.SARVAM_STREAMING_MODEL,
        "stream_type": config.SARVAM_STREAM_TYPE,
        "mode": config.SARVAM_STREAMING_MODE,
        "encoding": "linear16",
        "sample_rate": config.SAMPLE_RATE,
        "endpointing": "vad",
        "threshold": config.SARVAM_VAD_THRESHOLD,
        "silence_duration_ms": config.SARVAM_SILENCE_DURATION_MS,
        "min_speech_duration_ms": config.SARVAM_MIN_SPEECH_DURATION_MS,
        "return_timestamps": "true",
    }
    return f"{SARVAM_WS_BASE}/speech-to-text-realtime/ws?{urlencode(params)}"


def _legacy_url():
    language = config.SARVAM_LANGUAGE_CODE
    if language in ("", "auto"):
        language = "unknown"
    params = {
        "model": config.SARVAM_STREAMING_MODEL,
        "mode": config.SARVAM_STREAMING_MODE,
        "language_code": language,
        "sample_rate": config.SAMPLE_RATE,
        "input_audio_codec": "pcm_s16le",
        "vad_signals": "true",
        "flush_signal": "true",
        "high_vad_sensitivity": "true",
    }
    return f"{SARVAM_WS_BASE}/speech-to-text/ws?{urlencode(params)}"


class SarvamStream:
    """Async wrapper around one Sarvam WebSocket session."""

    def __init__(self, ws, kind):
        self.ws = ws
        self.kind = kind  # "realtime" | "legacy"

    @classmethod
    async def open(cls):
        if not config.SARVAM_API_KEY:
            raise SarvamStreamError(
                "SARVAM_API_KEY is not set. Add it to a .env file in the project root."
            )
        kind = config.SARVAM_STREAMING_ENDPOINT
        if kind not in ("realtime", "legacy"):
            raise SarvamStreamError(f"Unknown SARVAM_STREAMING_ENDPOINT: {kind!r}")
        url = _realtime_url() if kind == "realtime" else _legacy_url()
        try:
            ws = await _connect(url)
        except Exception as error:
            raise SarvamStreamError(f"Cannot open Sarvam streaming socket: {error}") from error
        return cls(ws, kind)

    async def send_audio(self, b64_pcm):
        if self.kind == "realtime":
            message = {"event": "audio_input", "audio": b64_pcm}
        else:
            message = {
                "audio": {
                    "data": b64_pcm,
                    "sample_rate": config.SAMPLE_RATE,
                    "encoding": "pcm_s16le",
                }
            }
        await self.ws.send(json.dumps(message))

    async def send_end(self):
        if self.kind == "realtime":
            message = {"event": "end"}
        else:
            message = {"flush": True}
        await self.ws.send(json.dumps(message))

    async def send_ping(self):
        if self.kind == "realtime":
            await self.ws.send(json.dumps({"event": "ping"}))

    async def recv(self):
        raw = await self.ws.recv()
        if raw is None:
            return None
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return {"event": "unknown", "raw": raw}

    async def close(self):
        try:
            await self.ws.close()
        except Exception:
            pass

    @property
    def closed(self):
        return self.ws.closed