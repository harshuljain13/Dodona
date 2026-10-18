"""The LiveKit worker: ears and mouth on the laptop, the brain on the GPU.

    make voice      starts the LiveKit server, this worker, and the web UI

Everything here runs locally. The one thing that leaves the machine is the
completion, and it goes to the gateway on 127.0.0.1:8080 — through guard,
admit, place and queue — because a completion that skips the serve path tells
us nothing about the infrastructure this project exists to measure.

AgentShip supplies the `llm=` slot only. LiveKit has no pipeline node to drop an
agent into: it has an AgentSession with stt / vad / llm / tts slots, so the
other three are LiveKit's own plugins and the agent is the LLM.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from agentship.spec import load_spec
from agentship_voice.adapters.livekit_adapter import LiveKitAdapter
from agentship_voice.turn import VoiceTurn
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli
from livekit.plugins import deepgram, silero

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dodona.voice")

SPEC = Path(__file__).with_name("dodona.yaml")


async def entrypoint(ctx: JobContext) -> None:
    """Join the room and run one Dodona session in it."""
    await ctx.connect()
    spec = load_spec(SPEC)
    turn = VoiceTurn(spec)

    session = AgentSession(
        # Purpose-built for streaming speech. A batch transcriber fed one VAD
        # segment at a time is markedly worse on live audio, and a misheard
        # question is answered confidently and wrongly.
        stt=deepgram.STT(model="nova-3", language="en"),
        # Local, free, no key. Decides when the speaker has stopped.
        vad=silero.VAD.load(),
        # The agent. Everything it knows about the cluster is `api_base`.
        llm=LiveKitAdapter().host(turn),
        # Slightly above the provider default, which is tuned for narration and
        # sounds sluggish when someone is waiting for their turn.
        tts=deepgram.TTS(model="aura-2-thalia-en"),
    )

    # LiveKit already reports what was actually spoken, including truncation on
    # interruption. The adapter subscribes to that rather than measuring a
    # second, worse answer of its own.
    LiveKitAdapter().witness(session, turn)

    await session.start(agent=Agent(instructions=spec.prompt), room=ctx.room)
    log.info("dodona voice session started in room %s", ctx.room.name)


if __name__ == "__main__":
    for key in ("DEEPGRAM_API_KEY", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        if not os.environ.get(key):
            raise SystemExit(f"{key} is not set — see app/README.md")
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
