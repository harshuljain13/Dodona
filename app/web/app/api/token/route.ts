import { AccessToken } from "livekit-server-sdk";
import { NextResponse } from "next/server";

// Minted server-side so the API secret never reaches the browser. The room name
// is per-visit: two tabs are two conversations, which is also how we get two
// concurrent sessions against the cluster without any extra machinery.
export async function GET(request: Request) {
  const url = new URL(request.url);
  const room = url.searchParams.get("room") ?? `dodona-${Date.now()}`;
  const identity = url.searchParams.get("identity") ?? `listener-${Math.random().toString(36).slice(2, 8)}`;

  const key = process.env.LIVEKIT_API_KEY;
  const secret = process.env.LIVEKIT_API_SECRET;
  if (!key || !secret) {
    return NextResponse.json(
      { error: "LIVEKIT_API_KEY / LIVEKIT_API_SECRET are not set — see app/README.md" },
      { status: 500 },
    );
  }

  const token = new AccessToken(key, secret, { identity, ttl: "30m" });
  token.addGrant({ room, roomJoin: true, canPublish: true, canSubscribe: true });

  return NextResponse.json({
    token: await token.toJwt(),
    url: process.env.NEXT_PUBLIC_LIVEKIT_URL ?? "ws://127.0.0.1:7880",
    room,
  });
}
