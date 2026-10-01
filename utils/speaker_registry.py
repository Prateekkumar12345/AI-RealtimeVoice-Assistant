"""Attribute transcript segments to numbered speakers.

The page mixes every participant into a single audio stream, so there is only
one Sarvam stream and the transcript text itself carries no speaker identity.
Speaker numbers are recovered from timing alone: the page reports
voice-activity windows per participant in *seconds of mixed audio sent*, which
is the same clock Sarvam timestamps its transcripts in (the legacy path in
server_app.py already computes `time.time() - conn_started_at` for exactly this
reason). Matching a transcript to the speaker with the greatest overlap inside
its time window therefore needs no wall-clock synchronisation at all.

This is honest heuristic diarisation, not source separation. It degrades in the
way diarisation normally does: if two people talk over each other for the whole
utterance, the utterance is attributed to whoever spoke longest during it.
"""

# Windows closer together than this are treated as one continuous utterance.
# A track's RMS dips below the off-threshold between words constantly, and
# without this "how are you" from one person becomes three separate windows.
MERGE_TOLERANCE_S = 0.35


class SpeakerRegistry:
    """Maps transcript time windows onto `Speaker 1`, `Speaker 2`, ..."""

    def __init__(self):
        self._windows = {}  # speaker -> sorted [(start, end), ...]
        self._peak = {}     # speaker -> highest concurrent overlap ever seen
        self._first_seen = {}  # speaker -> seconds of audio when first heard

    # -- ingestion ---------------------------------------------------------
    def add_windows(self, windows):
        """Absorb activity windows reported by the page.

        Returns the number of distinct speakers known afterwards, so callers can
        cheaply detect a newly arrived participant.
        """
        for window in windows or []:
            try:
                speaker = int(window.get("speaker"))
                start = float(window.get("startSec"))
                end = float(window.get("endSec"))
            except (TypeError, ValueError, AttributeError):
                # Malformed window: skip it rather than losing the whole batch.
                continue
            if speaker < 1 or not (end > start):
                continue
            self._first_seen.setdefault(speaker, start)
            self._windows.setdefault(speaker, []).append((start, end))
        for speaker in self._windows:
            self._windows[speaker] = self._merge(self._windows[speaker])
        return self.count

    @staticmethod
    def _merge(windows):
        """Sort and fuse overlapping/near-adjacent windows."""
        merged = []
        for start, end in sorted(windows):
            if merged and start - merged[-1][1] <= MERGE_TOLERANCE_S:
                if end > merged[-1][1]:
                    merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        return merged

    # -- attribution -------------------------------------------------------
    def resolve(self, start, end):
        """Return the speaker who holds the most of [start, end], else None.

        Ties are broken towards the speaker with the larger single window, so a
        brief interjection never steals an utterance from whoever is holding
        the floor.
        """
        if start is None:
            return self._most_recent()
        if end is None or end <= start:
            end = start
        best = None
        best_key = None
        for speaker, windows in self._windows.items():
            overlap = 0.0
            longest = 0.0
            for window_start, window_end in windows:
                shared = min(end, window_end) - max(start, window_start)
                if shared > 0:
                    overlap += shared
                longest = max(longest, min(end, window_end) - max(start, window_start))
            if overlap <= 0:
                continue
            key = (round(overlap, 3), round(longest, 3))
            if best_key is None or key > best_key:
                best_key = key
                best = speaker
        if best is not None:
            self._peak[best] = max(self._peak.get(best, 0.0), best_key[0])
        return best

    def _most_recent(self):
        """Speaker whose last window ends latest, for untimed transcripts."""
        best = None
        best_end = None
        for speaker, windows in self._windows.items():
            if not windows:
                continue
            if best_end is None or windows[-1][1] > best_end:
                best_end = windows[-1][1]
                best = speaker
        return best

    def most_recent_speaker(self):
        return self._most_recent()

    def latest_end(self):
        """End of the most recent activity window: how far we have heard into
        the meeting so far, in audio seconds."""
        ends = [windows[-1][1] for windows in self._windows.values() if windows]
        return max(ends) if ends else None

    def label(self, speaker):
        return f"Speaker {speaker}" if speaker else "Speaker"

    # -- reporting ---------------------------------------------------------
    @property
    def count(self):
        return len(self._windows)

    def speakers(self):
        """Per-speaker stats, ordered by who spoke first."""
        rows = []
        for speaker, windows in self._windows.items():
            speaking = sum(end - start for start, end in windows)
            rows.append({
                "speaker": speaker,
                "label": self.label(speaker),
                "speaking_ms": int(speaking * 1000),
                "turns": len(windows),
                "first_seen_sec": round(self._first_seen.get(speaker, 0.0), 2),
            })
        return sorted(rows, key=lambda row: (row["first_seen_sec"], row["speaker"]))

    def snapshot(self):
        return {
            "speaker_count": self.count,
            "speakers": self.speakers(),
        }
