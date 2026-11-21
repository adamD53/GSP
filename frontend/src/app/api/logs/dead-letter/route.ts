import { NextRequest, NextResponse } from "next/server";
import { getBackendBaseUrl } from "@/lib/backend";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const baseUrl = getBackendBaseUrl();
  const qs = request.nextUrl.searchParams.toString();
  const target = `${baseUrl}/logs/dead-letter${qs ? `?${qs}` : ""}`;

  try {
    const res = await fetch(target, {
      cache: "no-store",
      signal: AbortSignal.timeout(12000),
    });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to fetch dead-letter logs" },
      { status: 502 }
    );
  }
}
