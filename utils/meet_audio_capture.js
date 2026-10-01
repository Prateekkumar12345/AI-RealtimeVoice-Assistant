// Injected into the Google Meet page at document start (see utils/meet_bot.py).
//
// It records the audio of the *other participants* directly from the WebRTC
// streams inside the page, so it never touches the physical microphone or the
// speakers. The mixed audio is only ever connected to a silent sink (and the
// analyser for the level meter) and is deliberately NOT connected to
// AudioContext.destination, so this recorder can never play anything back and
// cannot cause an echo.
//
// The recording is captured as 16 kHz mono 16-bit PCM and handed to Python in
// small base64 chunks. utils/audio_recorder.py assembles those bytes into a
// standard WAV file, which is exactly the format/config Sarvam's batch STT
// recognizes best.
//
// Python drives it through window.__meetRec:
//   start()  -> Promise<{ok, error?}>   build the audio graph + start recording
//   drain()  -> string[]                base64 PCM chunks recorded since last call
//   stop()   -> bool                    ask the recorder to finish
//   status() -> {started, finished, error, peak, seconds, sources, tracksSeen,
//                speakers, speakerCount, audioSeconds}
//   drainActivity() -> Activity[]       voice-activity windows per speaker, since
//                                       the last call. Each is
//                                       {speaker, startSec, endSec} in *mixed
//                                       audio seconds* -- the same clock Sarvam
//                                       timestamps its transcripts in, so the
//                                       server can attribute a transcript to a
//                                       speaker with no wall-clock sync at all.
//   speakers() -> Speaker[]            {speaker, speakingMs, active, firstSeenSec}
(() => {
  if (window.self !== window.top || window.__meetRec) return;

  const TARGET_RATE = 16000;  // sample rate of the WAV we hand to Sarvam
  const CHUNK_BYTES = 32000;  // one second of 16 kHz mono 16-bit PCM

  // Voice-activity thresholds, as RMS of the track's live audio. The two
  // thresholds differ (hysteresis) so a speaker breathing or pausing between
  // words does not chop one utterance into three separate "speakers talking".
  const SPEECH_ON_RMS = 0.020;
  const SPEECH_OFF_RMS = 0.008;
  const ACTIVITY_POLL_MS = 200;
  const MIN_WINDOW_SEC = 0.25;  // ignore blips shorter than this

  const remoteTracks = new Map(); // track.id -> live remote audio MediaStreamTrack

  // Speaker identity. One Meet remote audio track ~= one participant, so the
  // track is the speaker key. Numbers are handed out in order of first voice,
  // which is what makes "Speaker 1" the first person actually heard rather than
  // whoever happened to join the call first.
  const speakersByTrack = new Map(); // track.id -> {speaker, ...}
  let nextSpeaker = 1;
  let outSamples = 0;   // 16 kHz mono samples pushed to Sarvam == the audio clock
  let activity = [];    // pending {speaker, startSec, endSec}

  const rec = {
    started: false,
    stopping: false,
    finished: false,
    error: null,
    startedAt: 0,
    peak: 0,
    chunks: [],
    graph: null,
    timers: [],
  };
  window.__meetRec = rec;

  let byteBuf = [];

  function audioNow() {
    return outSamples / TARGET_RATE;  // seconds of mixed audio sent so far
  }

  function pushPcmSample(sample) {
    outSamples += 1;
    byteBuf.push(sample & 0xff, (sample >> 8) & 0xff);
    if (byteBuf.length >= CHUNK_BYTES) flushBytes();
  }

  function flushBytes() {
    if (!byteBuf.length) return;
    let binary = '';
    for (let i = 0; i < byteBuf.length; i += 1) binary += String.fromCharCode(byteBuf[i]);
    byteBuf = [];
    try {
      const b64 = btoa(binary);
      if (b64) rec.chunks.push(b64);
    } catch (error) {
      rec.error = 'chunk encode failed: ' + error;
    }
  }

  function connectTrack(track) {
    const graph = rec.graph;
    if (!graph || graph.sources.has(track.id)) return;
    try {
      // createMediaStreamSource only reads the first audio track of a stream,
      // so give every remote track its own single-track stream.
      const source = graph.ctx.createMediaStreamSource(new MediaStream([track]));
      source.connect(graph.mix);
      graph.sources.set(track.id, source);

      // Per-track level tap. This is connected from the *source*, not from the
      // mix, so each speaker's level is measured in isolation. Tapping the mixed
      // bus instead would make two people talking look like one loud person.
      let entry = speakersByTrack.get(track.id);
      if (!entry) {
        entry = {
          // Left null until this person actually talks, so numbering follows the
          // order they are heard in rather than the order they happened to join
          // (a muted host would otherwise always be "Speaker 1").
          speaker: null,
          speakingMs: 0,
          active: false,
          firstSeenSec: audioNow(),
          _openAt: null,
        };
        speakersByTrack.set(track.id, entry);
      }
      const analyser = graph.ctx.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      // An analyser with no onward connection is not pulled, so it is anchored
      // to a zero gain. It is deliberately NOT connected to the destination.
      const anchor = graph.ctx.createGain();
      anchor.gain.value = 0;
      analyser.connect(anchor);
      anchor.connect(graph.sink);
      entry.analyser = analyser;
      entry.samples = new Float32Array(analyser.fftSize);

      track.addEventListener('ended', () => {
        closeSpeechWindow(entry, true);
        try { source.disconnect(); } catch (e) { /* already gone */ }
        try { analyser.disconnect(); anchor.disconnect(); } catch (e) { /* ignored */ }
        graph.sources.delete(track.id);
        remoteTracks.delete(track.id);
      });
    } catch (error) {
      rec.error = 'connect track failed: ' + error;
    }
  }

  // Turn a measured level into open/closed voice-activity windows. Windows are
  // only emitted once they are >= MIN_WINDOW_SEC long, so a single cough never
  // becomes its own speaker turn.
  function pollVoiceActivity() {
    if (!rec.graph) return;
    speakersByTrack.forEach((entry, trackId) => {
      if (!entry.analyser) return;
      entry.analyser.getFloatTimeDomainData(entry.samples);
      let sum = 0;
      for (let i = 0; i < entry.samples.length; i += 1) {
        sum += entry.samples[i] * entry.samples[i];
      }
      const rms = Math.sqrt(sum / entry.samples.length);
      if (!entry.active && rms >= SPEECH_ON_RMS) {
        entry.active = true;
        // Number speakers in the order they are first heard.
        if (entry.speaker === null) entry.speaker = nextSpeaker++;
        entry._openAt = audioNow();
      } else if (entry.active && rms < SPEECH_OFF_RMS) {
        closeSpeechWindow(entry, false);
      }
    });
  }

  function closeSpeechWindow(entry, force) {
    if (!entry.active) return;
    const closedAt = audioNow();
    const startedAt = entry._openAt === null ? closedAt : entry._openAt;
    entry.active = false;
    entry._openAt = null;
    const duration = closedAt - startedAt;
    entry.speakingMs += duration * 1000;
    if (force || duration >= MIN_WINDOW_SEC) {
      activity.push({
        speaker: entry.speaker,
        startSec: round3(startedAt),
        endSec: round3(closedAt),
      });
    }
  }

  function round3(value) {
    return Math.round(value * 1000) / 1000;
  }

  function noteTrack(track) {
    if (!track || track.kind !== 'audio' || track.readyState !== 'live') return;
    if (remoteTracks.has(track.id)) return;
    remoteTracks.set(track.id, track);
    connectTrack(track);
  }

  // 1) Remote audio tracks straight from every RTCPeerConnection the page opens.
  //    The 'track' event only ever fires for tracks *received* from other people.
  const NativePC = window.RTCPeerConnection;
  if (NativePC) {
    const HookedPC = function (...args) {
      const pc = new NativePC(...args);
      pc.addEventListener('track', (event) => noteTrack(event.track));
      return pc;
    };
    HookedPC.prototype = NativePC.prototype;
    Object.setPrototypeOf(HookedPC, NativePC); // keep statics such as generateCertificate
    window.RTCPeerConnection = HookedPC;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = HookedPC;
  }

  // 2) Safety net: streams attached to un-muted media elements. Muted elements
  //    are skipped because that is how a page shows its own self-preview.
  //    Same-origin iframes are scanned too (Meet puts its WebRTC layer in
  //    frames in some versions).
  function scanFrameElements(frameDoc) {
    if (!frameDoc) return;
    let elements;
    try {
      elements = frameDoc.querySelectorAll('audio, video');
    } catch (error) {
      return; // not this iframe's origin
    }
    elements.forEach((element) => {
      if (element.muted) return;
      const stream = element.srcObject;
      if (stream && typeof stream.getAudioTracks === 'function') {
        stream.getAudioTracks().forEach(noteTrack);
      }
    });
  }

  function scanMediaElements() {
    scanFrameElements(document);
    let frames;
    try {
      frames = document.querySelectorAll('iframe');
    } catch (error) {
      frames = [];
    }
    frames.forEach((frame) => {
      try {
        scanFrameElements(frame.contentDocument);
      } catch (error) {
        /* cross-origin frame; skipped */
      }
    });
  }

  // Downsample the mixed bus (mono) to 16 kHz and emit 16-bit little-endian PCM.
  function onAudioProcess(event) {
    const graph = rec.graph;
    if (!graph) return;
    const data = event.inputBuffer.getChannelData(0);
    const n = data.length;
    for (let i = 0; i < n; i++) {
      if (graph.inPos >= graph.nextOutAt) {
        graph.nextOutAt += graph.step;
        const s = data[i];
        const scaled = s < 0 ? s * 32768 : s * 32767;
        let v = scaled | 0;
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        pushPcmSample(v);
      }
      graph.inPos += 1;
    }
  }

  rec.start = async function () {
    if (rec.started) return { ok: true };
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      const ctx = new AudioCtx();
      // ctx.resume() can hang while the tab is throttled/backgrounded; never let
      // the start() promise wait on it forever. The graph is still built so PCM
      // begins flowing as soon as the context actually starts running.
      try {
        await Promise.race([
          ctx.resume(),
          new Promise((resolve) => setTimeout(resolve, 5000)),
        ]);
      } catch (error) {
        /* keep going; the 1s timer below retries resume() */
      }

      const mix = ctx.createGain();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 2048;

      const script = ctx.createScriptProcessor(4096, 1, 1);
      script.onaudioprocess = onAudioProcess;

      // The script node must be connected somewhere to keep processing, but only
      // to a silent sink — never to ctx.destination.
      const sink = ctx.createMediaStreamDestination();
      mix.connect(script);
      script.connect(sink);
      mix.connect(analyser);

      rec.graph = {
        ctx, mix, analyser, script, sink,
        sources: new Map(),
        step: ctx.sampleRate / TARGET_RATE,
        inPos: 0,
        nextOutAt: 0,
      };
      remoteTracks.forEach((track) => connectTrack(track));

      const samples = new Float32Array(analyser.fftSize);
      rec.timers.push(setInterval(() => {
        analyser.getFloatTimeDomainData(samples);
        for (let i = 0; i < samples.length; i++) {
          const value = Math.abs(samples[i]);
          if (value > rec.peak) rec.peak = value;
        }
      }, 200));
      rec.timers.push(setInterval(scanMediaElements, 1000));
      rec.timers.push(setInterval(pollVoiceActivity, ACTIVITY_POLL_MS));
      rec.timers.push(setInterval(() => {
        if (ctx.state !== 'running') ctx.resume().catch(() => { });
      }, 1000));
      scanMediaElements();

      rec.startedAt = Date.now();
      rec.started = true;
      return { ok: true };
    } catch (error) {
      rec.error = 'start failed: ' + error;
      return { ok: false, error: rec.error };
    }
  };

  rec.drain = function () {
    return rec.chunks.splice(0, rec.chunks.length);
  };

  rec.drainActivity = function () {
    return activity.splice(0, activity.length);
  };

  rec.speakers = function () {
    // Only people who have actually spoken get a number. A track that has been
    // connected but stayed silent is not a speaker yet.
    return Array.from(speakersByTrack.values()).filter((entry) => entry.speaker !== null)
      .map((entry) => ({
        speaker: entry.speaker,
        speakingMs: Math.round(entry.speakingMs),
        active: !!entry.active,
        firstSeenSec: entry.firstSeenSec,
      })).sort((a, b) => a.speaker - b.speaker);
  };

  rec.stop = function () {
    if (!rec.started || rec.stopping) return false;
    rec.stopping = true;
    rec.timers.forEach(clearInterval);
    rec.timers = [];
    flushBytes();
    // Close any window still open, so the last speaker of the call is not lost
    // just because they were still talking when we stopped.
    speakersByTrack.forEach((entry) => closeSpeechWindow(entry, true));
    rec.finished = true;
    try {
      const graph = rec.graph;
      if (graph) {
        graph.script.disconnect();
        graph.mix.disconnect();
        graph.sources.forEach((source) => {
          try { source.disconnect(); } catch (e) { /* ignored */ }
        });
        graph.sources.clear();
        graph.sink.disconnect();
      }
      if (rec.graph && rec.graph.ctx) {
        rec.graph.ctx.close().catch(() => { /* ignored */ });
      }
    } catch (error) {
      rec.error = 'stop cleanup failed: ' + error;
    }
    return true;
  };

  rec.status = function () {
    return {
      started: rec.started,
      finished: rec.finished,
      error: rec.error,
      peak: rec.peak,
      seconds: rec.started ? (Date.now() - rec.startedAt) / 1000 : 0,
      sources: rec.graph ? rec.graph.sources.size : 0,
      tracksSeen: remoteTracks.size,
      speakers: rec.speakers().length,
      speakerCount: nextSpeaker - 1,
      audioSeconds: audioNow(),
    };
  };
})();