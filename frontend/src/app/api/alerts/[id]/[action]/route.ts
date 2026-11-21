import { NextRequest, NextResponse } from "next/server";
import { getBackendBaseUrl } from "@/lib/backend";

export const dynamic = "force-dynamic";

const ALLOWED_ACTIONS = new Set(["ack", "resolve", "reopen"]);

export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string; action: string }> }
) {
  const { id, action } = await context.params;
  if (!ALLOWED_ACTIONS.has(action)) {
    return NextResponse.json({ error: "Invalid alert action" }, { status: 400 });
  }

  const baseUrl = getBackendBaseUrl();
  const target = `${baseUrl}/alerts/${encodeURIComponent(id)}/${action}`;

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
      { error: err instanceof Error ? err.message : "Failed to update alert" },
      { status: 502 }
    );
  }
}
