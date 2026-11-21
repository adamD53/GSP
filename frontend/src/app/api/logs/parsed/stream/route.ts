import { NextRequest, NextResponse } from "next/server";
import { getBackendBaseUrl } from "@/lib/backend";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const baseUrl = getBackendBaseUrl();
  const qs = request.nextUrl.searchParams.toString();
  const target = `${baseUrl}/logs/parsed/stream${qs ? `?${qs}` : ""}`;

  try {
    const upstream = await fetch(target, {
      cache: "no-store",
      signal: request.signal,
    });

    if (!upstream.ok || !upstream.body) {
      const detail = await upstream.text().catch(() => "Stream unavailable");
      return NextResponse.json({ error: detail }, { status: upstream.status || 502 });
    }

    return new NextResponse(upstream.body, {
      status: 200,
      headers: {
        "Content-Type": "application/x-ndjson; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
      },
    });
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      return new NextResponse(null, { status: 499 });
    }
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to open log stream" },
      { status: 502 }
    );
  }
}
