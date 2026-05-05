/**
 * Next.js API proxy: forwards query stream requests to FastAPI backend.
 * Preserves the Vercel AI SDK data-stream protocol.
 */
import { NextRequest, NextResponse } from "next/server";

// API_URL is for server-side (Docker: http://api:8000), falls back to NEXT_PUBLIC for local dev
const API_BASE =
  process.env.API_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:8000";

export async function POST(req: NextRequest) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${API_BASE}/queries/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : "Failed to reach API";
    return NextResponse.json({ error: msg }, { status: 502 });
  }

  if (!upstream.ok) {
    const err = await upstream.text();
    return NextResponse.json({ error: err }, { status: upstream.status });
  }

  // Pipe the stream through
  return new NextResponse(upstream.body, {
    headers: {
      "Content-Type": "text/plain",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  });
}
