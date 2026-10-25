import { NextResponse } from "next/server";

// Where the browser's microphone socket goes, and what it presents to get in.
//
// Server-side for the same reason the LiveKit token was: the key stays out of
// the client bundle. It is a dev key minted by run.sh, scoped to invoking this
// one agent on a service bound to loopback — not a credential to reuse.
export async function GET() {
  const port = process.env.AGENTSHIP_PORT ?? "8090";
  const agent = process.env.DODONA_AGENT ?? "dodona";
  const key = process.env.DODONA_DEV_KEY;

  if (!key) {
    return NextResponse.json(
      { error: "DODONA_DEV_KEY is not set — start the app with `make app`" },
      { status: 500 },
    );
  }

  // The browser cannot set headers on a WebSocket handshake, so AgentShip reads
  // the credential from the subprotocol list: ["bearer", <key>]. The page sends
  // it that way; this route only says what to send.
  return NextResponse.json({
    url: `ws://127.0.0.1:${port}/v1/agents/${agent}/voice`,
    // The same agent, reached by typing instead of talking. Both sockets end at
    // the same completion through the same gateway — which is the point: the
    // cluster cannot tell the two apart, so neither should the transcript.
    liveUrl: `ws://127.0.0.1:${port}/v1/agents/${agent}/live`,
    key,
    // Both directions. 24 kHz because that is what the synthesiser produces and
    // it will not resample — play it back as 16 kHz and the agent sounds slow
    // and deep, with nothing anywhere reporting an error.
    sampleRate: 24000,
  });
}
