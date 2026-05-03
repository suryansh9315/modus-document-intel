/**
 * Next.js API proxy: forwards query stream requests to FastAPI backend.
 * Preserves the Vercel AI SDK data-stream protocol.
 */
import { NextRequest, NextResponse } from "next/server";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function POST(req: NextRequest) {
  const body = await req.json();

  const upstream = await fetch(`${API_BASE}/queries/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

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
