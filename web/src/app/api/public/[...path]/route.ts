import { NextRequest, NextResponse } from "next/server";

import { API_BASE } from "@/lib/api/session";

/**
 * The public site's calls, forwarded verbatim.
 *
 * No credential is attached and none is expected -- this exists only so the
 * browser talks to one origin, which keeps CORS out of the picture and lets a
 * streamed answer pass through the same way the signed-in consoles' does.
 */
async function handler(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const target = `${API_BASE}/api/v1/public/organizations/${path
    .map(encodeURIComponent)
    .join("/")}${request.nextUrl.search}`;

  const upstream = await fetch(target, {
    method: request.method,
    headers: { "content-type": request.headers.get("content-type") ?? "application/json" },
    body: request.method === "GET" ? undefined : await request.arrayBuffer(),
    cache: "no-store",
  });

  const contentType = upstream.headers.get("content-type") ?? "";
  const headers = new Headers({ "content-type": contentType });

  if (contentType.includes("text/event-stream") && upstream.body) {
    headers.set("cache-control", "no-cache, no-transform");
    headers.set("x-accel-buffering", "no");
    return new NextResponse(upstream.body, { status: upstream.status, headers });
  }

  return new NextResponse(await upstream.arrayBuffer(), {
    status: upstream.status,
    headers,
  });
}

export const GET = handler;
export const POST = handler;
