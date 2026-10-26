import { NextRequest } from "next/server";

// Text turns, proxied to the service's SSE endpoint.
//
// WHY THIS EXISTS, rather than the browser opening /v1/agents/dodona/live
// directly: that websocket cannot be opened by a browser at all.
//
// A browser cannot set headers on a WebSocket handshake, so AgentShip carries
// the credential in the subprotocol list — `["bearer", <key>]`. RFC 6455 then
// requires the server to echo the selected subprotocol back, and a browser MUST
// fail the connection when it offered one and the server selected none.
// `/voice` does echo it:
//
//   voice.py:67   await websocket.accept(subprotocol="bearer" if "bearer" in offered else None)
//
// and `/live` does not:
//
//   live.py:54    await websocket.accept()
//
// So voice worked and text never did — in a browser. Every probe from Python
// succeeded, because Python's websockets client tolerates the missing echo,
// which made it look like a key or a restart problem for far too long.
//
// `POST /v1/agents/{name}:stream` carries the identical frames over SSE —
// session, tool_call, tool_result, content, done — and server-side we CAN set
// an Authorization header. So the transport changes and nothing else does: the
// browser gets the same event stream it was already written to parse.
export async function POST(request: NextRequest) {
  const port = process.env.AGENTSHIP_PORT ?? "8090";
  const agent = process.env.DODONA_AGENT ?? "dodona";
  const key = process.env.DODONA_DEV_KEY;

  if (!key) {
    return Response.json(
      { error: "DODONA_DEV_KEY is not set — start the app with `make app`" },
      { status: 500 },
    );
  }

  const body = await request.json();

  let upstream: Response;
  try {
    upstream = await fetch(`http://127.0.0.1:${port}/v1/agents/${agent}:stream`, {
      method: "POST",
      headers: {
        // The header the browser could not send. This is the whole point of
        // the proxy.
        Authorization: `Bearer ${key}`,
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({
        input: body.input,
        ...(body.session_id ? { session_id: body.session_id } : {}),
      }),
    });
  } catch (cause) {
    // The service is down. Say that, rather than letting the page invent a
    // reason — guessing at causes is what made this bug take a day.
    return Response.json(
      {
        error: `cannot reach the AgentShip service on 127.0.0.1:${port} — ` +
          `is \`make app\` still running? (${(cause as Error).message})`,
      },
      { status: 502 },
    );
  }

  if (!upstream.ok || !upstream.body) {
    const detail = await upstream.text().catch(() => "");
    return Response.json(
      { error: `the service refused the turn: ${upstream.status} ${detail.slice(0, 200)}` },
      { status: upstream.status },
    );
  }

  // Passed straight through, unbuffered. `no-transform` and the nginx hint stop
  // an intermediary collecting the stream and delivering it at the end, which
  // would turn a visible research trail back into a thirty-second blank screen.
  return new Response(upstream.body, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
