"""Live transcript state for one meeting session.

Sarvam's stream pushes *interim* parts as the user speaks. We only keep the
latest partial (the UI shows one floating line) and we only *commit* a segment
once Sarvam emits `transcript.final` for an utterance — that committed text is
what gets stored and what the LLM field extractor consumes.
"""
import threading
import time


class TranscriptManager:
    def __init__(self, session_id, started_at=None):
        self.session_id = session_id
        self.started_at = started_at or time.time()
        self._lock = threading.RLock()

        # Latest interim transcript (replaced on every partial event).
        self.partial = ""
        self.partial_language = None

        # Committed utterances: [{"text", "start", "end", "language"}]
        self.segments = []

        # Language seen on the most recent final (realtime auto-detect).
        self.language = None

        # Extraction cursor: index into self.segments already handed to the LLM.
        self.extracted_through = 0

        # Diagnostics surfaced to UI/stop flow.
        self.last_error = None
        self.audio_duration_s = None

    # ------------------------------------------------------------------ writes
    def on_partial(self, text, language=None):
        with self._lock:
            self.partial = (text or "").strip()
            self.partial_language = language
            if language:
                self.language = language

    def on_final(self, text, start=None, end=None, language=None):
        text = (text or "").strip()
        if not text:
            return None
        with self._lock:
            segment = {
                "text": text,
                "start": start,
                "end": end,
                "language": language,
            }
            self.segments.append(segment)
            self.partial = ""
            self.partial_language = None
            if language:
                self.language = language
            return segment

    def on_error(self, message, fatal=False):
        with self._lock:
            self.last_error = {"message": message, "fatal": fatal}

    def on_session_end(self, audio_duration_s=None):
        with self._lock:
            self.audio_duration_s = audio_duration_s
            self.partial = ""

    # ------------------------------------------------------------------- reads
    def pending_text(self):
        """Text of committed segments not yet sent to the field extractor."""
        with self._lock:
            if self.extracted_through >= len(self.segments):
                return None
            return "\n".join(
                s["text"] for s in self.segments[self.extracted_through:]
            )

    def mark_extracted(self):
        with self._lock:
            self.extracted_through = len(self.segments)

    def snapshot(self):
        with self._lock:
            return {
                "session_id": self.session_id,
                "started_at": self.started_at,
                "partial": self.partial,
                "language": self.language,
                "segments": list(self.segments),
                "extracted_through": self.extracted_through,
                "last_error": self.last_error,
                "audio_duration_s": self.audio_duration_s,
            }

    # --------------------------------------------------------------- formatting
    def to_display_transcript(self):
        """Same readable format as the batch project: one line per utterance."""
        lines = []
        for index, segment in enumerate(self.segments):
            label = f"[{_fmt_ts(segment.get('start'))}]" if segment.get("start") is not None else f"[{index + 1}]"
            lines.append(f"{label} Speaker: {segment['text']}")
        if self.partial:
            lines.append(f"[…] {self.partial}")
        return "\n\n".join(lines)


def _fmt_ts(value):
    value = int(value or 0)
    return f"{value // 3600:02d}:{(value % 3600) // 60:02d}:{value % 60:02d}"