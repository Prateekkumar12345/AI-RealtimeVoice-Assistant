import React, { useEffect, useMemo, useRef, useState } from "react";

const statusColors = {
  idle: "#6b7280",
  joining: "#f59e0b",
  recording: "#10b981",
  stopping: "#f59e0b",
  done: "#10b981",
  failed: "#dc2626",
};

function fmtTime(t) {
  if (t === null || t === undefined) return "";
  t = Math.max(0, Math.floor(t));
  const h = String(Math.floor(t / 3600)).padStart(2, "0");
  const m = String(Math.floor((t % 3600) / 60)).padStart(2, "0");
  const s = String(t % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

export default function App() {
  const [config, setConfig] = useState(null);
  const [meetingUrl, setMeetingUrl] = useState("");
  const [sessionId, setSessionId] = useState(null);
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState(null);
  const [partial, setPartial] = useState("");
  const [segments, setSegments] = useState([]);
  const [fields, setFields] = useState([]);
  const [wsState, setWsState] = useState("closed");
  const [sessions, setSessions] = useState([]);
  const [debug, setDebug] = useState(null);
  const [reconnecting, setReconnecting] = useState(false);
  const wsRef = useRef(null);
  const transcriptEndRef = useRef(null);
  const debugAtRef = useRef(0);
  // Latest status without re-binding the socket callbacks on every change.
  const statusRef = useRef(status);
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  useEffect(() => {
    fetch("/api/config")
      .then((r) => r.json())
      .then(setConfig)
      .catch(() => setConfig(null));
    fetch("/api/sessions")
      .then((r) => r.json())
      .then(setSessions)
      .catch(() => {});
    return () => {
      if (wsRef.current) wsRef.current.close();
    };
  }, []);

  // Auto-scroll the transcript pane as new lines land.
  useEffect(() => {
    if (transcriptEndRef.current) {
      transcriptEndRef.current.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [segments, partial]);

  const applySnapshot = (snap) => {
    setStatus(snap.status);
    setPartial(snap.partial || "");
    setSegments(snap.segments || []);
    setFields(snap.fields || []);
    setError(snap.error || null);
  };

  const connect = (sid) => {
    if (wsRef.current) wsRef.current.close();
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/session/${sid}`);
    wsRef.current = ws;
    setWsState("connecting");
    ws.onopen = () => {
      setWsState("open");
      setReconnecting(false);
    };
    ws.onclose = () => {
      setWsState("closed");
      // A recording session must always have a live socket; if the connection
      // drops (laptop sleep, network blip), keep trying until it is back.
      if (sid && ["recording", "stopping"].includes(statusRef.current)) {
        setReconnecting(true);
        setTimeout(() => connect(sid), 1500);
      }
    };
    ws.onerror = () => setWsState("error");
    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      switch (msg.type) {
        case "snapshot":
          applySnapshot(msg.session);
          break;
        case "status":
          setStatus(msg.status);
          setError(msg.error || null);
          break;
        case "partial":
          setPartial(msg.text);
          break;
        case "final":
          setSegments((prev) => [...prev, msg]);
          setPartial("");
          break;
        case "fields":
          setFields((prev) => {
            const seen = new Set(prev.map((f) => `${f.field}::${f.value}`));
            const additions = msg.fields.filter((f) => !seen.has(`${f.field}::${f.value}`));
            return [...prev, ...additions];
          });
          break;
        case "stt_error":
          // Fatal STT problems replace the error banner; transient ones (a
          // dropped socket) sit alongside the transcript without erasing it.
          setError(`Transcription error: ${msg.message}`);
          if (msg.fatal) setStatus("failed");
          break;
        case "session_end":
          setPartial("");
          break;
        case "done":
          applySnapshot(msg.session);
          setPartial("");
          break;
        case "debug":
          // Only shown for a few seconds after each update so the header stays calm.
          debugAtRef.current = Date.now();
          setDebug(msg);
          setTimeout(() => {
            if (Date.now() - debugAtRef.current >= 4900) setDebug(null);
          }, 5000);
          break;
        default:
          break;
      }
    };
  };

  const start = async () => {
    if (!meetingUrl.trim()) return;
    setError(null);
    try {
      const resp = await fetch("/api/sessions/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ meeting_url: meetingUrl.trim() }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setError(data.detail || "Failed to start session");
        return;
      }
      setSessionId(data.session_id);
      setSegments([]);
      setPartial("");
      setFields([]);
      connect(data.session_id);
    } catch (e) {
      setError(`Could not reach the server: ${e}`);
    }
  };

  const stop = async () => {
    if (!sessionId) return;
    try {
      await fetch(`/api/sessions/${sessionId}/stop`, { method: "POST" });
    } catch (e) {
      setError(`Stop request failed: ${e}`);
    }
  };

  const live = useMemo(() => ["recording", "stopping"].includes(status), [status]);

  return (
    <div style={{ fontFamily: "Inter, system-ui, sans-serif", maxWidth: 1000, margin: "0 auto", padding: 24 }}>
      <h1 style={{ marginBottom: 4 }}>Voicebot · Live Meeting Transcript</h1>
      <div style={{ color: "#6b7280", fontSize: 13, marginBottom: 24 }}>
        Chrome bot → WebRTC → PCM 16 kHz → Sarvam streaming STT → LLM form extraction → store
        {config && (
          <span style={{ marginLeft: 12 }}>
            · {config.streaming_model} · {config.streaming_endpoint} · language {config.language_code}
            · LLM {config.llm_provider_configured ? "online" : "regex-fallback"}
          </span>
        )}
      </div>

      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <section style={{ flex: 2, minWidth: 320 }}>
          <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
            <input
              value={meetingUrl}
              onChange={(e) => setMeetingUrl(e.target.value)}
              placeholder="https://meet.google.com/abc-defg-hij"
              style={{ flex: 1, padding: "10px 12px", borderRadius: 8, border: "1px solid #d1d5db" }}
            />
            <button
              onClick={start}
              disabled={live || status === "joining"}
              style={btnStyle("#10b981", "#0b8f68")}
            >
              Start
            </button>
            <button
              onClick={stop}
              disabled={!live}
              style={{ ...btnStyle("#ef4444", "#b91c1c"), ...(live ? {} : { opacity: 0.4 }) }}
            >
              Stop
            </button>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
            <span
              style={{
                width: 10, height: 10, borderRadius: "50%",
                background: statusColors[status] || "#6b7280",
                boxShadow: live ? "0 0 8px rgba(16,185,129,.7)" : "none",
              }}
            />
            <b>{status}</b>
            {sessionId && <span style={{ color: "#6b7280", fontSize: 12 }}>session {sessionId}</span>}
            <span style={{ marginLeft: "auto", fontSize: 12 }}>
              ws: {reconnecting ? "reconnecting…" : wsState}
            </span>
          </div>

          {error && (
            <div style={{ background: "#fef2f2", color: "#b91c1c", padding: "8px 12px", borderRadius: 8, marginBottom: 12, fontSize: 13 }}>
              {error}
            </div>
          )}

          {reconnecting && (
            <div style={{ background: "#fffbeb", color: "#92400e", padding: "8px 12px", borderRadius: 8, marginBottom: 12, fontSize: 13 }}>
              Connection to the live transcript stream was lost — reconnecting…
            </div>
          )}

          {debug && debug.capture && (
            <div style={{ background: "#f0f9ff", border: "1px solid #bae6fd", borderRadius: 8, padding: "8px 12px", marginBottom: 12, fontSize: 12, color: "#0369a1", fontFamily: "monospace" }}>
              <b>debug</b> · stt {debug.stt || "?"} {debug.sarvam_error ? `· err: ${debug.sarvam_error}` : ""} ·{" "}
              started {String(debug.capture.started)} · sources {debug.capture.sources} · tracks {debug.capture.tracksSeen} ·{" "}
              peak {Number(debug.capture.peak || 0).toFixed(2)} · {Math.round(debug.capture.seconds || 0)}s ·{" "}
              drains {debug.capture.drains ?? "?"} · {fmtWav(debug.wav_bytes)} bytes
            </div>
          )}

          {partial && (
            <div style={{ background: "#fefce8", border: "1px solid #fde047", padding: "10px 14px", borderRadius: 8, marginBottom: 12, fontStyle: "italic", color: "#713f12" }}>
              {partial}
            </div>
          )}

          <div style={{ background: "#f9fafb", border: "1px solid #e5e7eb", borderRadius: 12, padding: 16, minHeight: 200, maxHeight: 480, overflowY: "auto" }}>
            {segments.length === 0 && !partial && (
              <div style={{ color: "#9ca3af" }}>Transcript appears here as people speak…</div>
            )}
            {segments.map((seg, index) => (
              <div key={index} style={{ marginBottom: 10, lineHeight: 1.45 }}>
                <span style={{ color: "#6b7280", fontSize: 12, marginRight: 8 }}>
                  [{seg.start !== null && seg.start !== undefined ? fmtTime(seg.start) : index + 1}]
                </span>
                <b style={{ fontSize: 12, color: "#374151", marginRight: 6 }}>Speaker:</b>
                <span>{seg.text}</span>
              </div>
            ))}
            <div ref={transcriptEndRef} />
          </div>
        </section>

        <aside style={{ flex: 1, minWidth: 260 }}>
          <h3 style={{ marginTop: 0 }}>Extracted fields</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {fields.length === 0 && <div style={{ color: "#9ca3af", fontSize: 14 }}>Form values appear here…</div>}
            {fields.map((f, index) => (
              <div key={index} style={{ background: "#ecfdf5", border: "1px solid #a7f3d0", borderRadius: 8, padding: "8px 12px" }}>
                <div style={{ fontSize: 11, color: "#047857", textTransform: "uppercase", letterSpacing: 1 }}>
                  {f.field}
                </div>
                <div>{f.value}</div>
                <div style={{ fontSize: 11, color: "#6b7280" }}>confidence {Math.round((f.confidence || 0) * 100)}%</div>
              </div>
            ))}
          </div>

          <h3 style={{ marginTop: 24 }}>Past sessions ({sessions.length})</h3>
          <div style={{ fontSize: 13, color: "#6b7280" }}>
            {sessions.slice(0, 6).map((s) => (
              <div key={s.session_id} style={{ marginBottom: 4 }}>
                {s.session_id} · {s.status} · {(s.segments || []).length} segments
              </div>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}

function btnStyle(background, hover) {
  return {
    background,
    color: "#fff",
    border: "none",
    borderRadius: 8,
    padding: "10px 18px",
    cursor: "pointer",
    fontWeight: 600,
  };
}

function fmtWav(bytes) {
  if (bytes === null || bytes === undefined) return "?";
  const kb = bytes / 1024;
  if (kb >= 1024) return (kb / 1024).toFixed(1) + " MB";
  return Math.round(kb) + " KB";
}
