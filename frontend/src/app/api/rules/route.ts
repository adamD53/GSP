import { NextRequest, NextResponse } from "next/server";
import { getBackendBaseUrl } from "@/lib/backend";

export const dynamic = "force-dynamic";

export async function GET() {
  const baseUrl = getBackendBaseUrl();
  try {
    const res = await fetch(`${baseUrl}/rules`, {
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to fetch rules" },
      { status: 502 }
    );
  }
}

export async function PUT(request: NextRequest) {
  const baseUrl = getBackendBaseUrl();
  try {
    const body = await request.text();
    const res = await fetch(`${baseUrl}/rules`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to save rules" },
      { status: 502 }
    );
  }
}
