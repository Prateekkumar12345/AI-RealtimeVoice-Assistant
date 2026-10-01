"""Live transcript state for one meeting session.

Sarvam's stream pushes *interim* parts as the user speaks. We only keep the
latest partial (the UI shows one floating line) and we only *commit* a segment
once Sarvam emits `transcript.final` for an utterance — that committed text is
what gets stored and what the LLM field extractor consumes.

Speaker numbers are added on top by matching each segment's time window against
the per-participant voice activity the browser reports (SpeakerRegistry). Meet
mixes everyone into one audio stream, so this is the only place speaker identity
exists; see utils/speaker_registry.py for how the match is made.
"""
import threading
import time

from utils.speaker_registry import SpeakerRegistry

# A transcript can reach us before the activity window that explains it, because
# the two travel different paths (Sarvam round-trip vs. the browser poll).
# Rather than block the transcript on that, attribute what we can and re-try the
# unattributed remainder as later activity arrives.
RESOLVE_WINDOW_S = 90.0


class TranscriptManager:
    def __init__(self, session_id, started_at=None, speaker_attribution=True):
        self.session_id = session_id
        self.started_at = started_at or time.time()
        self._lock = threading.RLock()
        self.speaker_attribution = speaker_attribution
        self.speakers = SpeakerRegistry()

        # Segments whose speaker was not yet known, and updates to broadcast.
        self._unattributed = []
        self._speaker_updates = []

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
            if self.speaker_attribution:
                self._attribute(segment)
            return segment

    # ---------------------------------------------------------------- speakers
    def _attribute(self, segment):
        """Set speaker fields on a segment, queueing it for re-try if unknown."""
        speaker = self.speakers.resolve(segment.get("start"), segment.get("end"))
        if speaker:
            segment["speaker"] = speaker
            segment["speaker_label"] = self.speakers.label(speaker)
        else:
            segment["speaker"] = None
            segment["speaker_label"] = "Speaker"
            self._unattributed.append(segment)

    def feed_activity(self, windows):
        """Absorb voice-activity windows and re-try any deferred attribution."""
        with self._lock:
            before = self.speakers.count
            self.speakers.add_windows(windows)
            if self.speakers.count == before and not self._unattributed:
                return
            self._resolve_deferred()

    def _resolve_deferred(self):
        still_waiting = []
        for segment in self._unattributed:
            speaker = self.speakers.resolve(segment.get("start"), segment.get("end"))
            if speaker:
                segment["speaker"] = speaker
                segment["speaker_label"] = self.speakers.label(speaker)
                self._speaker_updates.append(segment)
            else:
                end = segment.get("end")
                # Give up once the utterance is old enough that no further
                # activity could plausibly explain it, so the queue cannot grow
                # without bound in a silent meeting.
                if end is None or self._age_of(end) > RESOLVE_WINDOW_S:
                    continue
                still_waiting.append(segment)
        self._unattributed = still_waiting

    def _age_of(self, end):
        """Seconds between a segment's end and the last audio we have seen."""
        reference = self.speakers.latest_end()
        if reference is None:
            return RESOLVE_WINDOW_S + 1.0
        return max(0.0, reference - float(end))

    def drain_speaker_updates(self):
        """Segments whose speaker was resolved late; caller re-broadcasts them."""
        with self._lock:
            updates = self._speaker_updates
            self._speaker_updates = []
            return updates

    def current_speaker(self):
        """Who was speaking most recently, for the live partial line."""
        with self._lock:
            if not self.speaker_attribution:
                return None
            return self.speakers.most_recent_speaker()

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
            snapshot = {
                "session_id": self.session_id,
                "started_at": self.started_at,
                "partial": self.partial,
                "language": self.language,
                "segments": list(self.segments),
                "extracted_through": self.extracted_through,
                "last_error": self.last_error,
                "audio_duration_s": self.audio_duration_s,
            }
            if self.speaker_attribution:
                snapshot["speaker"] = self.current_speaker()
                snapshot.update(self.speakers.snapshot())
            return snapshot

    # --------------------------------------------------------------- formatting
    def to_display_transcript(self):
        """Same readable format as the batch project: one line per utterance."""
        lines = []
        for index, segment in enumerate(self.segments):
            label = f"[{_fmt_ts(segment.get('start'))}]" if segment.get("start") is not None else f"[{index + 1}]"
            lines.append(f"{label} {segment.get('speaker_label', 'Speaker')}: {segment['text']}")
        if self.partial:
            speaking = ""
            if self.speaker_attribution:
                speaker = self.current_speaker()
                if speaker:
                    speaking = f"{self.speakers.label(speaker)}: "
            lines.append(f"[…] {speaking}{self.partial}")
        return "\n\n".join(lines)


def _fmt_ts(value):
    value = int(value or 0)
    return f"{value // 3600:02d}:{(value % 3600) // 60:02d}:{value % 60:02d}"