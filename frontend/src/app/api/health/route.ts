import { NextResponse } from "next/server";
import { getBackendBaseUrl } from "@/lib/backend";

export const dynamic = "force-dynamic";

export async function GET() {
  const baseUrl = getBackendBaseUrl();
  try {
    const res = await fetch(`${baseUrl}/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(4000),
    });
    const text = await res.text();
    if (!res.ok) {
      return NextResponse.json(
        { status: "degraded", detail: text, backendUrl: baseUrl },
        { status: res.status }
      );
    }
    return NextResponse.json({
      status: text.trim() === "ok" ? "ok" : "degraded",
      detail: text.trim(),
      backendUrl: baseUrl,
    });
  } catch (err) {
    return NextResponse.json(
      {
        status: "offline",
        detail: err instanceof Error ? err.message : "Backend unreachable",
        backendUrl: baseUrl,
      },
      { status: 503 }
    );
  }
}
