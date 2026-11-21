import { NextRequest, NextResponse } from "next/server";
import { getBackendBaseUrl } from "@/lib/backend";

export const dynamic = "force-dynamic";

// Cap concurrent live streams so stale/background tabs never exhaust
// Chromium's 6-connection HTTP/1.1 per-origin limit.
const MAX_ACTIVE_STREAMS = 2;
const activeStreams = new Set<AbortController>();

export async function GET(request: NextRequest) {
  const baseUrl = getBackendBaseUrl();
  const qs = request.nextUrl.searchParams.toString();
  const target = `${baseUrl}/logs/parsed/stream${qs ? `?${qs}` : ""}`;

  // Evict oldest active stream(s) before opening a new one
  while (activeStreams.size >= MAX_ACTIVE_STREAMS) {
    const oldest = activeStreams.values().next().value;
    if (!oldest) break;
    activeStreams.delete(oldest);
    oldest.abort();
  }

  const streamController = new AbortController();
  activeStreams.add(streamController);

  const onClientAbort = () => {
    activeStreams.delete(streamController);
    streamController.abort();
  };
  request.signal.addEventListener("abort", onClientAbort, { once: true });

  try {
    const upstream = await fetch(target, {
      cache: "no-store",
      signal: streamController.signal,
    });

    if (!upstream.ok || !upstream.body) {
      activeStreams.delete(streamController);
      const detail = await upstream.text().catch(() => "Stream unavailable");
      return NextResponse.json({ error: detail }, { status: upstream.status || 502 });
    }

    const upstreamReader = upstream.body.getReader();
    const body = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const { done, value } = await upstreamReader.read();
          if (done) {
            activeStreams.delete(streamController);
            controller.close();
            return;
          }
          controller.enqueue(value);
        } catch {
          activeStreams.delete(streamController);
          try {
            controller.close();
          } catch {
            // already closed
          }
        }
      },
      cancel() {
        activeStreams.delete(streamController);
        streamController.abort();
        void upstreamReader.cancel().catch(() => {});
      },
    });

    return new NextResponse(body, {
      status: 200,
      headers: {
        "Content-Type": "application/x-ndjson; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
      },
    });
  } catch (err) {
    activeStreams.delete(streamController);
    if (err instanceof Error && err.name === "AbortError") {
      return new NextResponse(null, { status: 499 });
    }
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to open log stream" },
      { status: 502 }
    );
  }
}
