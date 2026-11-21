import { NextRequest, NextResponse } from "next/server";
import { getBackendBaseUrl } from "@/lib/backend";

export const dynamic = "force-dynamic";

export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string; stepIndex: string }> }
) {
  const { id, stepIndex } = await context.params;
  const baseUrl = getBackendBaseUrl();
  const target = `${baseUrl}/alerts/${encodeURIComponent(id)}/steps/${encodeURIComponent(stepIndex)}/execute`;

  try {
    const body = await request.text();
    const res = await fetch(target, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body || "{}",
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
      { error: err instanceof Error ? err.message : "Failed to execute step" },
      { status: 502 }
    );
  }
}
