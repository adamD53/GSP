import { NextRequest, NextResponse } from "next/server";
import { getBackendBaseUrl } from "@/lib/backend";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const baseUrl = getBackendBaseUrl();
  const qs = request.nextUrl.searchParams.toString();
  const target = `${baseUrl}/logs${qs ? `?${qs}` : ""}`;

  try {
    const res = await fetch(target, {
      cache: "no-store",
      signal: AbortSignal.timeout(12000),
    });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to fetch raw logs" },
      { status: 502 }
    );
  }
}

export async function POST(request: NextRequest) {
  const baseUrl = getBackendBaseUrl();
  const qs = request.nextUrl.searchParams.toString();
  const target = `${baseUrl}/logs${qs ? `?${qs}` : ""}`;
  const contentType = request.headers.get("content-type") || "text/plain";

  try {
    const body = await request.text();
    const res = await fetch(target, {
      method: "POST",
      headers: { "Content-Type": contentType },
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to ingest log" },
      { status: 502 }
    );
  }
}
