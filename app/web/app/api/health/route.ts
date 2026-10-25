import { NextResponse } from "next/server";

// Is the serve path answering? Asked server-side for two reasons: the gateway
// sends no CORS headers, so the browser cannot ask it directly, and this way the
// UI asks exactly the question voice.py asks — does `/v1/models` answer — rather
// than inferring the cluster's state from a failure somewhere else.
export async function GET() {
  const base = process.env.DODONA_API_BASE ?? "http://127.0.0.1:8080/v1";
  try {
    const res = await fetch(`${base}/models`, {
      signal: AbortSignal.timeout(2000),
      cache: "no-store",
    });
    return NextResponse.json({ cluster: res.ok, base });
  } catch {
    // Refused, timed out, no tunnel — all the same answer to the listener, who
    // can only do one thing about it.
    return NextResponse.json({ cluster: false, base });
  }
}
