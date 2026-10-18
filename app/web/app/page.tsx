"use client";

import {
  RoomAudioRenderer,
  RoomContext,
  useVoiceAssistant,
  useTrackTranscription,
  useLocalParticipant,
} from "@livekit/components-react";
import { Room, Track } from "livekit-client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Orb } from "@/components/Orb";

export default function Page() {
  const room = useMemo(() => new Room({ adaptiveStream: true, dynacast: true }), []);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connect = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch("/api/token");
      const body = await res.json();
      if (!res.ok) throw new Error(body.error ?? "could not mint a token");
      await room.connect(body.url, body.token);
      // Publishing the mic is what starts the conversation; the agent is already
      // in the room waiting for audio.
      await room.localParticipant.setMicrophoneEnabled(true);
      setConnected(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [room]);

  const disconnect = useCallback(async () => {
    await room.disconnect();
    setConnected(false);
  }, [room]);

  useEffect(() => () => void room.disconnect(), [room]);

  return (
    <RoomContext.Provider value={room}>
      <div className="shell">
        <div className="stage">
          <div className="brand">
            Dodona<span>voice deep research, on one GPU</span>
          </div>
          {connected ? (
            <Live onLeave={disconnect} />
          ) : (
            <>
              <Orb state="disconnected" level={0} />
              <div className="state">{error ? "could not connect" : "ready"}</div>
              <button className="cta" onClick={connect}>
                Start talking
              </button>
              {error && (
                <div className="empty" style={{ color: "#f85149" }}>
                  {error}
                </div>
              )}
            </>
          )}
        </div>
        <Transcript connected={connected} />
      </div>
      <RoomAudioRenderer />
    </RoomContext.Provider>
  );
}

// What the agent is doing, in words a listener would use.
const SAID: Record<string, string> = {
  listening: "listening",
  thinking: "researching",
  speaking: "speaking",
  initializing: "waking up",
  disconnected: "not connected",
  connecting: "connecting",
};

function Live({ onLeave }: { onLeave: () => void }) {
  const { state, audioTrack } = useVoiceAssistant();
  const [level, setLevel] = useState(0);

  // Drive the orb from the agent's actual output. A fake animation would look
  // the same whether or not the GPU had answered, which is the one thing this
  // demo is meant to show.
  useEffect(() => {
    const mediaTrack = audioTrack?.publication?.track?.mediaStreamTrack;
    if (!mediaTrack) return;
    const ctx = new AudioContext();
    const source = ctx.createMediaStreamSource(new MediaStream([mediaTrack]));
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    const bins = new Uint8Array(analyser.frequencyBinCount);
    let raf = 0;
    const tick = () => {
      analyser.getByteFrequencyData(bins);
      let sum = 0;
      for (const b of bins) sum += b;
      setLevel(Math.min(1, sum / bins.length / 110));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      source.disconnect();
      void ctx.close();
    };
  }, [audioTrack]);

  return (
    <>
      <Orb state={state as never} level={level} />
      <div className="state">{SAID[state] ?? state}</div>
      <button className="cta ghost" onClick={onLeave}>
        End session
      </button>
    </>
  );
}

function Transcript({ connected }: { connected: boolean }) {
  const { agentTranscriptions } = useVoiceAssistant();
  // The real participant, not a cast. Passing `undefined as never` here
  // typechecked and then crashed the prerender reading `.identity` — the type
  // was satisfied and the contract was not.
  const { localParticipant, microphoneTrack } = useLocalParticipant();
  const mine = useTrackTranscription(
    localParticipant && microphoneTrack
      ? {
          publication: microphoneTrack,
          source: Track.Source.Microphone,
          participant: localParticipant,
        }
      : undefined,
  );

  // One list, ordered by when each segment arrived, so the conversation reads
  // the way it was spoken rather than agent-then-user in two columns.
  const turns = [
    ...(mine?.segments ?? []).map((s) => ({ who: "you" as const, ...s })),
    ...(agentTranscriptions ?? []).map((s) => ({ who: "agent" as const, ...s })),
  ].sort((a, b) => a.firstReceivedTime - b.firstReceivedTime);

  return (
    <aside className="transcript">
      <header>Transcript</header>
      <div className="log">
        {turns.length === 0 ? (
          <div className="empty">
            {connected
              ? "Ask a research question out loud. Dodona will say it is looking, then answer from sources it has read."
              : "Nothing yet."}
          </div>
        ) : (
          turns.map((t) => (
            <div key={t.id} className={`turn ${t.who}`}>
              <span className="who">{t.who === "you" ? "You" : "Dodona"}</span>
              {t.text}
            </div>
          ))
        )}
      </div>
    </aside>
  );
}
