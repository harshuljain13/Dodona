"use client";

import { useEffect, useRef } from "react";

type State = "disconnected" | "connecting" | "initializing" | "listening" | "thinking" | "speaking";

// One hue per state, so the orb says what the agent is doing without a label.
// Thinking is the one that matters: on a research turn the listener waits in
// silence for seconds, and an orb that looks identical to idle reads as broken.
const HUE: Record<State, number> = {
  disconnected: 220,
  connecting: 220,
  initializing: 220,
  listening: 190,
  thinking: 275,
  speaking: 155,
};

export function Orb({ state, level }: { state: State; level: number }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // Read through refs so the animation loop is started once and never restarted
  // by a prop change — remounting it mid-sentence makes the orb stutter.
  const stateRef = useRef(state);
  const levelRef = useRef(level);
  stateRef.current = state;
  levelRef.current = level;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const size = 320;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);

    let frame = 0;
    let smoothed = 0;
    let raf = 0;

    const draw = () => {
      const s = stateRef.current;
      const hue = HUE[s];
      // Ease towards the measured level. Raw amplitude jitters at frame rate and
      // makes the orb twitch rather than breathe.
      smoothed += (levelRef.current - smoothed) * 0.12;
      frame += 1;

      ctx.clearRect(0, 0, size, size);
      const cx = size / 2;
      const cy = size / 2;

      // A slow breath when idle, so a waiting agent still looks alive.
      const breath = Math.sin(frame / 55) * 0.02 + 1;
      const thinking = s === "thinking";
      const pulse = thinking ? Math.sin(frame / 14) * 0.05 + 1 : 1;
      const base = 64 * breath * pulse;
      const radius = base + smoothed * 54;

      // Outer glow.
      const glow = ctx.createRadialGradient(cx, cy, radius * 0.5, cx, cy, radius * 2.4);
      glow.addColorStop(0, `hsla(${hue}, 85%, 62%, 0.34)`);
      glow.addColorStop(1, `hsla(${hue}, 85%, 62%, 0)`);
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(cx, cy, radius * 2.4, 0, Math.PI * 2);
      ctx.fill();

      // Rings that ride the voice. Three of them, offset, so loud speech reads
      // as motion rather than just a bigger circle.
      for (let i = 0; i < 3; i += 1) {
        const phase = frame / (30 + i * 11) + i * 2.1;
        const wobble = Math.sin(phase) * (3 + smoothed * 16);
        ctx.beginPath();
        ctx.arc(cx, cy, radius + i * 13 + wobble, 0, Math.PI * 2);
        ctx.strokeStyle = `hsla(${hue}, 80%, ${66 - i * 6}%, ${0.4 - i * 0.11})`;
        ctx.lineWidth = 1.6;
        ctx.stroke();
      }

      // Core.
      const core = ctx.createRadialGradient(cx - radius * 0.3, cy - radius * 0.35, 2, cx, cy, radius);
      core.addColorStop(0, `hsla(${hue}, 95%, 78%, 0.98)`);
      core.addColorStop(0.55, `hsla(${hue}, 85%, 58%, 0.92)`);
      core.addColorStop(1, `hsla(${hue}, 80%, 40%, 0.88)`);
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.fill();

      raf = requestAnimationFrame(draw);
    };

    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, []);

  return <canvas ref={canvasRef} style={{ width: 320, height: 320 }} aria-hidden />;
}
