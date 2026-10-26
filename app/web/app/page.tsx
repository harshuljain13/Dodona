"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Orb } from "@/components/Orb";

// The agent's side of the conversation, as AgentShip reports it. Six event
// kinds arrive as JSON text on the same socket that carries audio as binary —
// a browser tells them apart by payload type, which is the whole protocol.
type State = "disconnected" | "connecting" | "initializing" | "listening" | "thinking" | "speaking";
// One thing the agent did on the way to answering. A research turn makes several
// of these and they are the most interesting part of the screen: without them a
// thirty-second silence is indistinguishable from a hang, and you cannot tell an
// agent that searched from one that answered out of the model's memory.
type Step = {
  at: number;
  kind: "thinking" | "tool" | "result" | "guard";
  label: string;
  detail?: string;
};

// A page the agent found or read. `web_search` returns
// {query, provider, results: [{title, url, description}]} and `scrape_url` is
// handed one url — so the agent's actual reading list is in the event stream
// and was being summarised down to "3 results" and thrown away.
//
// `read` separates the two, and it is the distinction that matters in a deep
// research answer: a search HIT is a title the agent saw, a scrape is a page it
// actually took the content of. An agent citing something it only saw the
// snippet of is the failure this makes visible.
type Source = {
  url: string;
  title?: string;
  description?: string;
  read?: boolean;
};

// `pending` marks a turn still being streamed into. `at` is when it started, not
// when it finished, so the gap to the next timestamp is how long the turn took.
type Turn = {
  id: number;
  who: "you" | "agent";
  text: string;
  at: number;
  pending?: boolean;
  steps?: Step[];
  sources?: Source[];
  //: Spoken rather than typed. Voice arrives as several `said` chunks that are
  //: one answer, so they join; a typed turn must not absorb them.
  spoken?: boolean;
};

//: Hostname only, for a compact byline under a title. A full URL wraps over
//: three lines in a narrow column and buries the title it belongs to.
function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url.slice(0, 40);
  }
}

// Pull the reading list out of a tool result. Tolerant on purpose: the payload
// arrives as a JSON string, the provider may change, and a research turn that
// renders no sources is worse than one that renders a few imperfectly.
function sourcesFrom(raw: unknown): Source[] {
  let data: unknown = raw;
  if (typeof raw === "string") {
    try {
      data = JSON.parse(raw);
    } catch {
      return [];
    }
  }
  if (typeof data !== "object" || data === null) return [];
  const results = (data as Record<string, unknown>).results;
  if (!Array.isArray(results)) return [];
  return results
    .filter((r): r is Record<string, unknown> => typeof r === "object" && r !== null)
    .map((r) => ({
      url: String(r.url ?? ""),
      title: typeof r.title === "string" ? r.title : undefined,
      description: typeof r.description === "string" ? r.description : undefined,
    }))
    .filter((s) => s.url.startsWith("http"));
}

// The first words of `voice.fallback_text` in app/dodona.yaml. A failed voice
// turn is spoken, not signalled — the adapter pushes this as a TextFrame and the
// socket relays it as a normal `said` — so the agreed sentence is the only way
// the browser can know. Prefix, not equality: TTS framing can append.
//
// If you change the line in dodona.yaml, change it here. That coupling is ugly
// and it is the whole mechanism, so it is written down rather than inferred.
const FAILED_TURN = "Sorry, that turn failed.";

// Local time, seconds included: a research turn is tens of seconds and minute
// resolution would show three steps all claiming the same moment.
//
// Called only from turns created by interaction, never during server rendering —
// `toLocaleTimeString` on the server and in the browser disagree, and that is a
// hydration mismatch. The transcript is empty on first paint, so there is
// nothing to disagree about.
const clock = (at: number) =>
  new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

// A tool result is a JSON blob of whole scraped pages. Nobody reads that in a
// transcript, but "3 results · firecrawl" vs "0 results" is the difference
// between a working key and a silent fallback to the model's own memory.
function summariseResult(raw: unknown): string | undefined {
  if (raw == null) return undefined;
  let data: unknown = raw;
  if (typeof raw === "string") {
    try {
      data = JSON.parse(raw);
    } catch {
      return raw.length > 90 ? `${raw.slice(0, 90)}…` : raw;
    }
  }
  if (typeof data !== "object" || data === null) return String(data).slice(0, 90);
  const o = data as Record<string, unknown>;
  const results = Array.isArray(o.results) ? o.results : undefined;
  const parts: string[] = [];
  if (results) parts.push(`${results.length} result${results.length === 1 ? "" : "s"}`);
  if (typeof o.provider === "string") parts.push(o.provider);
  // Firecrawl returns a page as markdown rather than a result list.
  if (!results && typeof o.markdown === "string") {
    parts.push(`${o.markdown.length.toLocaleString()} chars`);
  }
  if (typeof o.error === "string") parts.push(o.error.slice(0, 70));
  return parts.length ? parts.join(" · ") : undefined;
}

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
// `channel` because the same failures reach here from two different sockets, and
// a typed message that failed used to be reported as "the voice socket did not
// open" — which sends you looking at the microphone for a text bug.
function explain(raw: string, channel: "voice" | "text" = "voice"): string {
  if (/permission|NotAllowed|denied/i.test(raw))
    return "Microphone permission was refused. Allow the mic for localhost:3000, then start again.";
  if (/4403/.test(raw))
    // Reload, not restart. The key is minted per `make app` run and handed to
    // the browser by /api/voice, so a tab left open across a restart presents
    // the previous run's key — and a reload is all it takes to get the current
    // one. The old advice here sent people to restart the thing that was fine.
    return "The service refused the key. It is minted fresh by each `make app` run, so reload this page — a tab open across a restart still holds the old one.";
  if (/4404/.test(raw))
    return "This agent has no `voice:` block, so there is nothing to talk to. Check app/dodona.yaml.";
  if (/4503/.test(raw))
    return "The voice packages are missing on the server: pip install -r requirements-app.txt.";
  if (/DODONA_DEV_KEY|not set/i.test(raw)) return raw;
  if (channel === "text")
    return "The text socket did not open. If `make app` restarted since this page loaded, reload — the key is per run. Otherwise, is the AgentShip service still running?";
  return "The voice socket did not open. The AgentShip service is part of `make app` — is it still running?";
}

// Light / dark, remembered.
//
// The attribute is already correct before this mounts — layout.tsx sets it in a
// blocking script — so this only has to READ it and offer the swap. Starting
// from `null` and filling it in an effect keeps the server and client render
// identical, which is what stops the toggle itself causing the hydration
// mismatch the script exists to avoid.
function ThemeToggle() {
  const [theme, setTheme] = useState<"light" | "dark" | null>(null);

  useEffect(() => {
    const current = document.documentElement.getAttribute("data-theme");
    setTheme(current === "light" ? "light" : "dark");
  }, []);

  const flip = () => {
    const next = theme === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("dodona-theme", next);
    } catch {
      // Private browsing. The theme still applies for this session.
    }
    setTheme(next);
  };

  // Renders nothing until the effect has run, so the first client render
  // matches the server's.
  if (!theme) return <button className="theme" aria-hidden suppressHydrationWarning />;

  return (
    <button className="theme" onClick={flip} aria-label={`Switch to ${theme === "light" ? "dark" : "light"} theme`}>
      {theme === "light" ? "◐ Dark" : "◑ Light"}
    </button>
  );
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
  // True while the agent is researching — not while it is speaking.
  //
  // A ref, not state: the capture callback is created once when the microphone
  // starts, so it would close over whatever `state` was at that moment and read
  // "connecting" for the rest of the call.
  const researching = useRef(false);
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
  // run `make fleet-runpod-ssh`, and expect the page to notice without a reload.
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
      return [
        ...prev,
        patch({ id: nextId.current++, who: "agent", text: "", at: Date.now(), pending: true }),
      ];
    });
  }, []);

  // Appends to the open turn's trail. Separate from `extend` only because every
  // caller would otherwise repeat the spread.
  const addStep = useCallback(
    (kind: Step["kind"], label: string, detail?: string) => {
      extend((t) => ({
        ...t,
        steps: [...(t.steps ?? []), { at: Date.now(), kind, label, detail }],
      }));
    },
    [extend],
  );

  // Merge by url, because a research turn searches several times and the same
  // page comes back for more than one query. Later information wins: a page
  // first seen as a search hit and later scraped becomes `read`, and that
  // upgrade is the whole point of keeping them in one list.
  const addSources = useCallback(
    (found: Source[]) => {
      if (!found.length) return;
      extend((t) => {
        const by = new Map((t.sources ?? []).map((s) => [s.url, s]));
        for (const s of found) {
          const prev = by.get(s.url);
          by.set(s.url, prev ? { ...prev, ...s, read: prev.read || s.read } : s);
        }
        return { ...t, sources: [...by.values()] };
      });
    },
    [extend],
  );

  // One typed turn, over SSE.
  //
  // NOT the /live websocket, which a browser cannot open at all: AgentShip
  // carries the credential in the subprotocol list because a browser cannot set
  // handshake headers, and RFC 6455 makes a browser fail the connection when it
  // offered a subprotocol and the server selected none. `/voice` echoes it back
  // (voice.py:67); `/live` does not (live.py:54). So voice worked and text never
  // did — while every probe from Python succeeded, because Python's client
  // tolerates the missing echo.
  //
  // `/api/chat` proxies to `:stream`, which carries the identical frames and
  // lets the server set an Authorization header. The frame handling below is
  // unchanged, because the frames are unchanged.
  const runTurn = useCallback(
    async (text: string) => {
      // A hang must become an error, not an eternal "thinking".
      //
      // Silence is the worst failure this page can produce: a stale bundle, a
      // dead service and a slow research turn all look identical, so there is
      // nothing to act on. The abort fires only if NOTHING arrives — each frame
      // resets it — so a genuinely long turn is never cut off, and a turn that
      // produced no first frame in 90 seconds says so.
      const ctrl = new AbortController();
      let idle = setTimeout(() => ctrl.abort(), 90_000);
      const alive = () => {
        clearTimeout(idle);
        idle = setTimeout(() => ctrl.abort(), 90_000);
      };

      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input: text, session_id: session.current ?? undefined }),
        signal: ctrl.signal,
      }).catch((e) => {
        throw new Error(
          e?.name === "AbortError"
            ? "no response in 90s. If the page was open across a `make app` restart, " +
              "hard-reload it (Cmd-Shift-R) — the bundle is stale."
            : `cannot reach /api/chat: ${e?.message ?? e}`,
        );
      });
      if (!res.ok || !res.body) {
        const why = await res.json().catch(() => ({}));
        throw new Error(why.error ?? `the service answered ${res.status}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      // SSE frames are separated by a blank line, and a chunk can split one in
      // half — so hold the tail until its terminator arrives rather than
      // parsing whatever happened to land in this read.
      for (;;) {
        const { done, value } = await reader.read().catch((e) => {
          throw new Error(
            e?.name === "AbortError"
              ? "the stream stalled with no frame for 90s — the agent may be stuck on a tool"
              : `the stream broke: ${e?.message ?? e}`,
          );
        });
        if (done) break;
        alive();
        // Strip CR before anything else.
        //
        // The service frames with sse-starlette, which separates events with
        // CRLF — the bytes are `}}\r\n\r\n event:`. Splitting on "\n\n" can
        // therefore NEVER match, because there are no two consecutive newlines
        // anywhere in the stream. The buffer grew for ever, not one frame was
        // parsed, and the turn sat on "thinking" until it timed out.
        //
        // curl appeared to prove the endpoint worked, every time, because curl
        // prints bytes and does not parse them. AgentShip Studio has had this
        // line since it was written, with the comment that gave it away.
        buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          // Join every data: line in the block. SSE allows an event to be split
          // across several, and taking only the first silently truncates it.
          const payload = frame
            .split("\n")
            .filter((l) => l.startsWith("data:"))
            .map((l) => l.slice(5).trim())
            .join("\n");
          if (!payload) continue;
          const line = `data:${payload}`;
          let f: { type?: string; data?: Record<string, unknown> };
          try {
            f = JSON.parse(line.slice(5).trim());
          } catch {
            continue;
          }
          const d = (f.data ?? {}) as Record<string, string & Record<string, string>>;
          // Every frame is proof of life, including a tool_call that will take
          // 20 seconds to come back.
          alive();
      switch (f.type) {
        case "session":
          // Kept and sent back on every later turn, which is what gives a typed
          // conversation the same memory a spoken one gets from one socket.
          session.current = d.session_id;
          break;
        case "token":
        case "content":
          extend((t) => ({ ...t, text: t.text + (d.content ?? "") }));
          break;
        case "tool_call":
          // A scrape is the agent committing to read one page. Record it now,
          // so the source list shows what it is working on rather than only
          // what it finished — a 20-second scrape is otherwise a silent gap.
          if (typeof d.args?.url === "string") {
            addSources([{ url: d.args.url, read: true }]);
          }
          // The query, not just the tool name. "web_search…" tells you it did
          // something; `web_search · llm inference` tells you whether it
          // understood the question — which is the thing you actually want to
          // see when it comes back with a bad answer.
          addStep(
            "tool",
            String(d.tool ?? d.name ?? "tool"),
            typeof d.args?.query === "string" ? d.args.query
              : typeof d.args?.url === "string" ? d.args.url
              : d.args ? JSON.stringify(d.args).slice(0, 120)
              : undefined,
          );
          break;
        case "tool_result":
          // This event was arriving and being dropped, so the trail stopped at
          // "searching" and never said whether anything came back. A search that
          // returns nothing and a search that was never made look identical
          // otherwise, and only one of them is a missing API key.
          addStep("result", String(d.tool ?? "tool"), summariseResult(d.result));
          addSources(sourcesFrom(d.result));
          break;
        case "guard":
          addStep("guard", "guardrail", String(d.reason ?? d.detail ?? "refused"));
          break;
        case "error":
          extend((t) => ({ ...t, text: t.text || `— ${d.detail ?? "the turn failed"}`, pending: false }));
          // Abandon the thread, do not reuse it.
          //
          // `durability: checkpoint` keys the conversation on this id, so a turn
          // that died between a tool call and its result leaves an AIMessage
          // with tool_calls and no ToolMessage in the saved state. langgraph
          // validates history on entry, so EVERY later turn on this id then
          // fails with "Found AIMessages with tool_calls that do not have a
          // corresponding ToolMessage" — the session is poisoned, permanently,
          // and the agent apologises forever while the fleet is perfectly
          // healthy. One interruption mid-research is enough to cause it.
          //
          // Dropping the id costs the thread's memory and buys a working agent.
          session.current = null;
          setSending(false);
          break;
        case "done":
          extend((t) => ({ ...t, pending: false }));
          setSending(false);
          break;
      }
        }
      }
      clearTimeout(idle);
    },
    [extend, addStep, addSources],
  );

  const send = useCallback(async () => {
    const text = typed.trim();
    if (!text || sending) return;
    setTyped("");
    setTurns((prev) => [...prev, { id: nextId.current++, who: "you", text, at: Date.now() }]);
    setSending(true);
    try {
      // Before any frame arrives. A research turn can take tens of seconds to
      // its first tool call, and an empty screen for that long reads as broken.
      addStep("thinking", "thinking");
      await runTurn(text);
      // `done` clears this, but a stream that ends without one must not leave
      // the composer disabled for the rest of the session.
      setSending(false);
    } catch (e) {
      const detail = e instanceof Error ? e.message : String(e);
      // Show the RAW failure next to the friendly line, always.
      //
      // This swallowed it, and that cost real time: the page said "the text
      // socket did not open — did make app restart?" while the service was
      // healthy and the keys matched, and the one thing that would have
      // identified the cause — the close code and reason the server sent — was
      // thrown away. A guess rendered as an explanation is worse than no
      // explanation, because it sends you to check the thing it names.
      setTurns((prev) => [
        ...prev,
        {
          id: nextId.current++,
          who: "agent",
          text: `— ${explain(detail, "text")}`,
          at: Date.now(),
          steps: [{ at: Date.now(), kind: "guard" as const, label: "turn failed", detail }],
        },
      ]);
      setSending(false);
    }
  }, [typed, sending, runTurn, addStep]);

  // Only the voice half. The text socket outlives a spoken session on purpose:
  // ending the call should not throw away the conversation.
  const stop = useCallback(() => {
    // So the next call starts with an open microphone whatever state this one
    // ended in.
    researching.current = false;
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
            // Anything the agent says means the turn is over, including the
            // apology for a failed one. Without this a turn that dies without
            // ever emitting `idle` would leave the microphone shut for the rest
            // of the call — a worse bug than the one the gate fixes, and a
            // silent one.
            if (msg.type === "said") researching.current = false;
            setTurns((prev) => {
              // ONE answer, not one bubble per clause.
              //
              // VoiceTurn.say yields chunks as they arrive rather than joining
              // them, so TTS can start on the first clause while the model is
              // still producing the rest — time-to-first-audio is what the
              // listener experiences as latency. Each chunk reaches us as its
              // own `said`, so appending a turn per event split one answer into
              // four bubbles stamped the same second, which reads as the agent
              // repeating itself.
              //
              // Join onto the open spoken turn instead. `heard` always starts a
              // new one: that is a different speaker.
              const last = prev[prev.length - 1];
              if (msg.type === "said" && last?.who === "agent" && last.spoken) {
                const joined = `${last.text} ${String(msg.text ?? "").trim()}`.trim();
                return [...prev.slice(0, -1), { ...last, text: joined }];
              }
              return [
                ...prev,
                {
                  id: nextId.current++,
                  who: msg.type === "heard" ? "you" : "agent",
                  text: msg.text,
                  at: Date.now(),
                  // Marks a turn that is being spoken, so the next chunk knows
                  // to join it. A typed turn must never absorb a spoken clause.
                  spoken: msg.type === "said",
                  // A failed turn arrives as an ordinary `said` — the adapter
                  // catches the exception, speaks the fallback and emits nothing
                  // structured — so the agreed string in dodona.yaml's
                  // `fallback_text` is the only signal there is. Matching our own
                  // text, not a library default, which is why it is set there.
                  steps:
                    msg.type === "said" && String(msg.text ?? "").startsWith(FAILED_TURN)
                      ? [{ at: Date.now(), kind: "guard" as const,
                           label: "session needs reconnecting",
                           detail: "Interruptions are on, so a stray word during a silent research turn can cancel it. Press Stop, then Start Talking — this call's history cannot be continued." }]
                      : undefined,
                },
              ];
            });
          } else if (msg.type === "idle") {
            researching.current = false;
            setState("listening");
          } else if (["listening", "thinking", "speaking"].includes(msg.type)) {
            // Only `thinking` closes the microphone. `speaking` deliberately
            // does not: cutting the agent off mid-answer is the barge-in worth
            // having, and it is safe — there is no half-finished tool call to
            // strand, because the tools already ran.
            researching.current = msg.type === "thinking";
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
      // Ask for the socket's rate up front. Chrome and Safari both honour this
      // now, and when they do `ratio` is 1 and there is no resampling at all —
      // which is the only resampling that is certainly correct. The fallback
      // below handles a browser that ignores it.
      const ctx = new AudioContext({ sampleRate: cfg.sampleRate });
      capture.current = ctx;
      const source = ctx.createMediaStreamSource(stream);
      // ScriptProcessor rather than an AudioWorklet: a worklet needs a second
      // file fetched over the network before the first word can be captured,
      // and this runs for one listener on localhost. Deprecated, not broken.
      //
      // 4096 frames, not 2048: this callback runs on the MAIN thread, so it
      // competes with React rendering the transcript. At 2048 it had ~43ms to
      // run; a re-render that overran that dropped audio, and dropped audio is
      // a clipped first word, not an error anybody sees.
      const node = ctx.createScriptProcessor(4096, 1, 1);
      const ratio = ctx.sampleRate / cfg.sampleRate;
      node.onaudioprocess = (e) => {
        if (ws.readyState !== WebSocket.OPEN) return;
        // Stop feeding the recogniser while the agent is researching.
        //
        // Interruptions stay ON — this is not that switch. The problem is that
        // pipecat drops its word floor to 1 whenever the bot is not SPEAKING,
        // and a research turn is half a minute of not speaking. Observed in one
        // 30-second window: two interruptions, both from 2-word INTERIM
        // transcripts, each cancelling the run between a tool result arriving
        // and it being written to the checkpoint. langgraph then refuses every
        // later turn on that session for a dangling tool call.
        //
        // An interruption during research cannot do anything useful — there is
        // no speech to cut off — so the only thing it can do is destroy the
        // turn. Sending nothing is how the browser declines to offer it.
        // Barge-in during the answer is untouched.
        // Send SILENCE while researching — never nothing.
        //
        // This used to `return`, which stopped the stream mid-flight. A
        // streaming recogniser carries acoustic and timing state across a
        // continuous stream, so a hole is not a pause to it — it is a
        // discontinuity. The transcript was accurate at first and degraded
        // after the first research turn, worsening with each one: "LLM
        // inference" came back as "alarm Wind friends".
        //
        // Silence does what the gate was for — no words reach the recogniser,
        // so a stray syllable cannot cancel a turn between a tool call and its
        // result — while the stream stays unbroken and the timeline keeps
        // advancing at real-time cadence.
        const gated = researching.current;
        const input = e.inputBuffer.getChannelData(0);
        const out16 = new Int16Array(Math.floor(input.length / ratio));
        for (let i = 0; i < out16.length; i++) {
          let s: number;
          if (ratio === 1) {
            // The good case, and the common one: nothing to do at all.
            s = input[i];
          } else if (ratio > 1) {
            // Downsample. Average the window rather than picking one sample
            // from it: taking every Nth sample is decimation with no low-pass,
            // so everything above half the target rate folds back into the band
            // — at 48k -> 24k that is everything above 12 kHz, which is where
            // sibilants live. "s", "sh" and "f" become mid-frequency mush and
            // the recogniser guesses at them.
            const from = Math.floor(i * ratio);
            const to = Math.min(input.length, Math.max(from + 1, Math.floor((i + 1) * ratio)));
            let sum = 0;
            for (let j = from; j < to; j++) sum += input[j];
            s = sum / (to - from);
          } else {
            // Upsample, by interpolating between neighbours.
            //
            // The previous version wrote SILENCE here: with ratio < 1 the
            // window collapses to zero width, the averaging loop never runs,
            // and it emitted a zero. Every other sample was a hole — a square
            // wave at half the rate, audible as a buzz and nonsense to a
            // recogniser. Interpolation cannot invent detail, but it is not
            // noise.
            const at = i * ratio;
            const lo = Math.floor(at);
            const hi = Math.min(input.length - 1, lo + 1);
            const frac = at - lo;
            s = input[lo] * (1 - frac) + input[hi] * frac;
          }
          s = Math.max(-1, Math.min(1, s));
          // `gated` zeroes the sample but still emits it, so the buffer keeps
          // its length and the stream keeps its cadence.
          out16[i] = gated ? 0 : s < 0 ? s * 0x8000 : s * 0x7fff;
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
          <b>Dodona</b>
          <span>voice deep research, on one GPU</span>
        </div>
        <ThemeToggle />
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
                  <code>make fleet-runpod</code> brings the fleet up and opens the tunnel. The
                  gateway is expected on 127.0.0.1:8080 — if the fleet is already running,{" "}
                  <code>make fleet-runpod-ssh</code> reopens the tunnel on its own.
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
                <span className="who">
                  {t.who === "you" ? "You" : "Dodona"}
                  <time dateTime={new Date(t.at).toISOString()}>{clock(t.at)}</time>
                </span>
                {/* Above the answer, in the order it happened: what it decided to
                    look up, and what came back. */}
                {t.steps?.map((s, i) => (
                  <span key={i} className={`step ${s.kind}`}>
                    <time dateTime={new Date(s.at).toISOString()}>{clock(s.at)}</time>
                    <b>{s.label}</b>
                    {s.detail && <i>{s.detail}</i>}
                  </span>
                ))}
                {t.text}
                {/* A turn with steps but no text yet is mid-research, which the
                    steps already say. A turn with neither has not started. */}
                {t.pending && !t.text && !t.steps?.length && (
                  <span className="step thinking">
                    <b>working…</b>
                  </span>
                )}
                {/* What it actually read. Under the answer, not above it: the
                    answer is what you asked for, and the sources are how you
                    check it — which is the order you want them in.

                    `read` pages first. A page the agent scraped is evidence it
                    reasoned over; a search hit is a title it saw and may have
                    cited from a one-line snippet, and those are not the same
                    claim. Spelling out which is which is the point. */}
                {!!t.sources?.length && (
                  <div className="sources">
                    <span className="sources-head">
                      {t.sources.filter((s) => s.read).length} read ·{" "}
                      {t.sources.length} found
                    </span>
                    {[...t.sources]
                      .sort((a, b) => Number(!!b.read) - Number(!!a.read))
                      .map((src) => (
                        <a
                          key={src.url}
                          className={`source ${src.read ? "read" : ""}`}
                          href={src.url}
                          target="_blank"
                          rel="noreferrer noopener"
                        >
                          <b>{src.title || hostOf(src.url)}</b>
                          <span className="host">
                            {hostOf(src.url)}
                            {src.read && <i> · read in full</i>}
                          </span>
                          {src.description && <em>{src.description}</em>}
                        </a>
                      ))}
                  </div>
                )}
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
