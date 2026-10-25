"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Orb } from "@/components/Orb";

// The agent's side of the conversation, as AgentShip reports it. Six event
// kinds arrive as JSON text on the same socket that carries audio as binary —
// a browser tells them apart by payload type, which is the whole protocol.
type State = "disconnected" | "connecting" | "initializing" | "listening" | "thinking" | "speaking";
// `pending` marks a turn still being streamed into; `note` is what the agent is
// doing rather than saying — a tool call, or a guardrail refusing it.
type Turn = { id: number; who: "you" | "agent"; text: string; pending?: boolean; note?: string };

// What the agent is doing, in words a listener would use.
const SAID: Record<State, string> = {
  listening: "listening",
  thinking: "researching",
  speaking: "speaking",
  initializing: "waking up",
  disconnected: "not connected",
  connecting: "connecting",
};

// Four failures reach this one button and have nothing to do with each other.
// Naming the wrong one sends you to the wrong machine — a media failure is this
// laptop, and the cluster is a separate thing with its own banner.
function explain(raw: string): string {
  if (/permission|NotAllowed|denied/i.test(raw))
    return "Microphone permission was refused. Allow the mic for localhost:3000, then start again.";
  if (/4403/.test(raw))
    return "The voice service refused the key. `make app` mints it — restart rather than reloading.";
  if (/4404/.test(raw))
    return "This agent has no `voice:` block, so there is nothing to talk to. Check app/dodona.yaml.";
  if (/4503/.test(raw))
    return "The voice packages are missing on the server: pip install -r requirements-app.txt.";
  if (/DODONA_DEV_KEY|not set/i.test(raw)) return raw;
  return "The voice socket did not open. The AgentShip service is part of `make app` — is it still running?";
}

export default function Page() {
  const [state, setState] = useState<State>("disconnected");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [level, setLevel] = useState(0);
  const [error, setError] = useState<{ said: string; detail: string } | null>(null);
  // Null until asked, so a slow first check does not accuse the cluster.
  const [cluster, setCluster] = useState<boolean | null>(null);

  const [typed, setTyped] = useState("");
  const [sending, setSending] = useState(false);

  const socket = useRef<WebSocket | null>(null);
  const capture = useRef<AudioContext | null>(null);
  const playback = useRef<AudioContext | null>(null);
  const mic = useRef<MediaStream | null>(null);
  // The text socket, opened on the first message and kept for the session so
  // the agent keeps its short-term memory across turns. Separate from the voice
  // socket because they are separate endpoints, not separate agents.
  const live = useRef<WebSocket | null>(null);
  const session = useRef<string | null>(null);
  // When the next received chunk should start. Audio arrives faster than it
  // plays, so each buffer is scheduled after the last rather than at "now" —
  // otherwise they all fire at once and the agent sounds like a crowd.
  const playhead = useRef(0);
  const nextId = useRef(0);

  // Polled, not read once: the usual sequence is to start the app, see this,
  // run `make ssh`, and expect the page to notice without a reload.
  useEffect(() => {
    const ask = () =>
      fetch("/api/health")
        .then((r) => r.json())
        .then((b) => setCluster(Boolean(b.cluster)))
        .catch(() => setCluster(false));
    ask();
    const id = setInterval(ask, 10_000);
    return () => clearInterval(id);
  }, []);

  // Grow the agent's in-flight turn, or start one. Tokens arrive one at a time
  // and a new turn is only appended when the last one has been closed by `done`.
  const extend = useCallback((patch: (t: Turn) => Turn) => {
    setTurns((prev) => {
      const last = prev[prev.length - 1];
      if (last?.who === "agent" && last.pending) {
        return [...prev.slice(0, -1), patch(last)];
      }
      return [...prev, patch({ id: nextId.current++, who: "agent", text: "", pending: true })];
    });
  }, []);

  const openLive = useCallback(async (): Promise<WebSocket> => {
    if (live.current?.readyState === WebSocket.OPEN) return live.current;
    const res = await fetch("/api/voice");
    const cfg = await res.json();
    if (!res.ok) throw new Error(cfg.error ?? "no live config");

    const ws = new WebSocket(cfg.liveUrl, ["bearer", cfg.key]);
    live.current = ws;

    ws.onmessage = (ev) => {
      const f = JSON.parse(ev.data as string);
      const d = f.data ?? {};
      switch (f.type) {
        case "session":
          // Kept and sent back on every later turn, which is what gives a typed
          // conversation the same memory a spoken one gets from one socket.
          session.current = d.session_id;
          break;
        case "token":
        case "content":
          extend((t) => ({ ...t, text: t.text + (d.content ?? ""), note: undefined }));
          break;
        case "tool_call":
          // Worth showing. In voice this is the silence the agent apologises
          // for; in text it is the research actually happening.
          extend((t) => ({ ...t, note: `${d.name ?? d.tool ?? "tool"}…` }));
          break;
        case "guard":
          extend((t) => ({ ...t, note: `guardrail: ${d.reason ?? d.detail ?? "refused"}` }));
          break;
        case "error":
          extend((t) => ({ ...t, text: t.text || `— ${d.detail ?? "the turn failed"}`, pending: false }));
          setSending(false);
          break;
        case "done":
          extend((t) => ({ ...t, pending: false, note: undefined }));
          setSending(false);
          break;
      }
    };
    ws.onclose = () => {
      live.current = null;
      setSending(false);
    };

    await new Promise<void>((resolve, reject) => {
      ws.onopen = () => resolve();
      ws.addEventListener("close", (e) => reject(new Error(`${e.code} ${e.reason}`.trim())), {
        once: true,
      });
    });
    return ws;
  }, [extend]);

  const send = useCallback(async () => {
    const text = typed.trim();
    if (!text || sending) return;
    setTyped("");
    setTurns((prev) => [...prev, { id: nextId.current++, who: "you", text }]);
    setSending(true);
    try {
      const ws = await openLive();
      ws.send(JSON.stringify({ input: text, session_id: session.current ?? undefined }));
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      setTurns((prev) => [
        ...prev,
        { id: nextId.current++, who: "agent", text: `— ${explain(detail)}` },
      ]);
      setSending(false);
    }
  }, [typed, sending, openLive]);

  // Only the voice half. The text socket outlives a spoken session on purpose:
  // ending the call should not throw away the conversation.
  const stop = useCallback(() => {
    socket.current?.close(1000, "listener left");
    socket.current = null;
    mic.current?.getTracks().forEach((t) => t.stop());
    mic.current = null;
    void capture.current?.close();
    void playback.current?.close();
    capture.current = playback.current = null;
    setState("disconnected");
    setLevel(0);
  }, []);

  useEffect(() => stop, [stop]);

  const start = useCallback(async () => {
    setError(null);
    setState("connecting");
    try {
      const res = await fetch("/api/voice");
      const cfg = await res.json();
      if (!res.ok) throw new Error(cfg.error ?? "no voice config");

      const stream = await navigator.mediaDevices.getUserMedia({
        // Browser-side cleanup, because the speaker and the microphone are a
        // foot apart: without echo cancellation the agent hears itself and
        // interrupts its own sentence.
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      mic.current = stream;

      // ["bearer", key] — a browser cannot set headers on a handshake, so
      // AgentShip promotes the second subprotocol to an Authorization header.
      const ws = new WebSocket(cfg.url, ["bearer", cfg.key]);
      ws.binaryType = "arraybuffer";
      socket.current = ws;

      await new Promise<void>((resolve, reject) => {
        ws.onopen = () => resolve();
        // A close before open carries the server's reason; after open it is a
        // hang-up. Only the first is an error worth showing.
        ws.onclose = (e) => reject(new Error(`${e.code} ${e.reason}`.trim()));
      });

      // Output first: createBuffer takes the rate the *data* is in, and the
      // context resamples on playback. That is why nothing here has to know
      // what rate the hardware chose.
      const out = new AudioContext();
      playback.current = out;
      playhead.current = out.currentTime;

      ws.onmessage = (ev) => {
        if (typeof ev.data === "string") {
          const msg = JSON.parse(ev.data);
          if (msg.type === "heard" || msg.type === "said") {
            setTurns((prev) => [
              ...prev,
              { id: nextId.current++, who: msg.type === "heard" ? "you" : "agent", text: msg.text },
            ]);
          } else if (msg.type === "idle") {
            setState("listening");
          } else if (["listening", "thinking", "speaking"].includes(msg.type)) {
            setState(msg.type as State);
          }
          // `timings` also arrives, carrying the turn's trace. Not shown here —
          // `make logs` has it, and a latency table belongs beside the run report.
          return;
        }

        const pcm = new Int16Array(ev.data as ArrayBuffer);
        if (!pcm.length) return;
        const buf = out.createBuffer(1, pcm.length, cfg.sampleRate);
        const channel = buf.getChannelData(0);
        let peak = 0;
        for (let i = 0; i < pcm.length; i++) {
          channel[i] = pcm[i] / 32768;
          const abs = Math.abs(channel[i]);
          if (abs > peak) peak = abs;
        }
        // Drive the orb from the audio actually played. A fake animation would
        // look the same whether or not the GPU had answered, which is the one
        // thing this demo exists to show.
        setLevel(Math.min(1, peak * 1.6));
        const node = out.createBufferSource();
        node.buffer = buf;
        node.connect(out.destination);
        const at = Math.max(out.currentTime, playhead.current);
        node.start(at);
        playhead.current = at + buf.duration;
      };
      ws.onclose = () => stop();

      // Input. The hardware rate is whatever it is — 48 kHz on most Macs — so
      // decimate to the rate the socket runs at rather than asking the browser
      // for a rate it may quietly ignore.
      const ctx = new AudioContext();
      capture.current = ctx;
      const source = ctx.createMediaStreamSource(stream);
      // ScriptProcessor rather than an AudioWorklet: a worklet needs a second
      // file fetched over the network before the first word can be captured,
      // and this runs for one listener on localhost. Deprecated, not broken.
      const node = ctx.createScriptProcessor(2048, 1, 1);
      const ratio = ctx.sampleRate / cfg.sampleRate;
      node.onaudioprocess = (e) => {
        if (ws.readyState !== WebSocket.OPEN) return;
        const input = e.inputBuffer.getChannelData(0);
        const out16 = new Int16Array(Math.floor(input.length / ratio));
        for (let i = 0; i < out16.length; i++) {
          const s = Math.max(-1, Math.min(1, input[Math.floor(i * ratio)]));
          out16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        ws.send(out16.buffer);
      };
      source.connect(node);
      // Through a silent gain node: a ScriptProcessor only fires while it is
      // connected to a destination, and connecting it directly would play the
      // listener's own microphone back at them.
      const mute = ctx.createGain();
      mute.gain.value = 0;
      node.connect(mute);
      mute.connect(ctx.destination);

      setState("listening");
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      setError({ said: explain(detail), detail });
      stop();
    }
  }, [stop]);

  const connected = state !== "disconnected" && state !== "connecting";

  return (
    <div className="shell">
      <div className="stage">
        <div className="brand">
          Dodona<span>voice deep research, on one GPU</span>
        </div>
        {connected ? (
          <>
            <Orb state={state} level={level} />
            <div className="state">{SAID[state]}</div>
            <button className="cta ghost" onClick={stop}>
              End session
            </button>
          </>
        ) : (
          <>
            <Orb state="disconnected" level={0} />
            <div className="state">{error ? "voice path failed" : "ready"}</div>
            <button className="cta" onClick={start}>
              Start talking
            </button>
            {error && (
              <div className="notice bad">
                {error.said}
                <span className="detail">{error.detail}</span>
              </div>
            )}
            {/* Said before the click, not after: the mic, the recogniser and
                the voice all work without the cluster, and only the answer
                does not. Finding that out by being answered with silence is
                how this gets mistaken for a broken app. */}
            {cluster === false && (
              <div className="notice warn">
                Local inference cluster unavailable. You can talk and be heard, but nothing
                can answer — the agent will say so too.
                <span className="detail">
                  <code>make cluster &amp;&amp; make ssh</code> — the gateway is expected on 127.0.0.1:8080
                </span>
              </div>
            )}
          </>
        )}
      </div>

      <aside className="transcript">
        <header>Transcript</header>
        <div className="log">
          {turns.length === 0 ? (
            <div className="empty">
              Ask a research question — type it, or press <b>Start talking</b> and say it.
              Dodona looks it up, reads the sources, and answers from them.
            </div>
          ) : (
            turns.map((t) => (
              <div key={t.id} className={`turn ${t.who}`}>
                <span className="who">{t.who === "you" ? "You" : "Dodona"}</span>
                {t.text}
                {t.note && <span className="note">{t.note}</span>}
              </div>
            ))
          )}
        </div>
        {/* Typing reaches the same agent over the same gateway as speaking: one
            is not a fallback for the other, and the cluster cannot tell them
            apart. It also works before the microphone is connected, which makes
            it the quickest way to prove the serve path end to end. */}
        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <input
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={sending ? "Dodona is working…" : "Type a question"}
            aria-label="Type a question"
          />
          <button type="submit" disabled={!typed.trim() || sending}>
            Send
          </button>
        </form>
      </aside>
    </div>
  );
}
