import { NextRequest, NextResponse } from "next/server";

import {
  API_BASE,
  API_PREFIX,
  REFRESH_PATH,
  type Console,
  readSession,
  setSession,
} from "./session";

/**
 * Single-flight refresh, keyed by the refresh token being spent.
 *
 * The backend rotates the refresh token, so two requests that both notice a 401
 * and both refresh would spend the same token twice. Concurrent callers await
 * the first one's promise instead of starting their own.
 *
 * This map is per Node process, which is correct for one dev server. Running
 * several would need the lock somewhere shared.
 */
const inflight = new Map<string, Promise<TokenPair | null>>();

type TokenPair = { access_token: string; refresh_token: string; expires_in?: number };

async function doRefresh(c: Console, refreshToken: string): Promise<TokenPair | null> {
  const response = await fetch(`${API_BASE}${REFRESH_PATH[c]}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
    cache: "no-store",
  });
  if (!response.ok) return null;
  return (await response.json()) as TokenPair;
}

function refresh(c: Console, refreshToken: string): Promise<TokenPair | null> {
  const key = `${c}:${refreshToken}`;
  const existing = inflight.get(key);
  if (existing) return existing;

  const started = doRefresh(c, refreshToken).finally(() => inflight.delete(key));
  inflight.set(key, started);
  return started;
}

const HOP_BY_HOP = new Set(["connection", "keep-alive", "transfer-encoding", "host"]);

function forwardHeaders(request: NextRequest, token: string | null): Headers {
  const headers = new Headers();
  request.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key) && key !== "cookie" && key !== "content-length") {
      headers.set(key, value);
    }
  });
  if (token) headers.set("authorization", `Bearer ${token}`);
  return headers;
}

/**
 * Forward one browser request to the backend, attaching the console's token.
 *
 * On a 401 the token is refreshed once and the request replayed. A second 401
 * means the session is genuinely over, and the 401 is passed through so the
 * page can send the user to its own login.
 */
export async function proxy(
  request: NextRequest,
  c: Console,
  path: string[],
): Promise<NextResponse> {
  const session = await readSession(c);
  const search = request.nextUrl.search;
  const target = `${API_BASE}${API_PREFIX[c]}/${path.join("/")}${search}`;

  // Read the body once: a replay after refresh cannot re-consume a stream.
  const body =
    request.method === "GET" || request.method === "HEAD"
      ? undefined
      : await request.arrayBuffer();

  const send = (token: string | null) =>
    fetch(target, {
      method: request.method,
      headers: forwardHeaders(request, token),
      body,
      cache: "no-store",
      redirect: "manual",
    });

  let upstream = await send(session.access);

  if (upstream.status === 401 && session.refresh) {
    const rotated = await refresh(c, session.refresh);
    if (rotated) {
      await setSession(c, rotated);
      upstream = await send(rotated.access_token);
    }
  }

  const contentType = upstream.headers.get("content-type") ?? "";

  // Server-sent events and file downloads must flow through, not be collected.
  // Buffering an SSE response means the browser sees nothing until the stream
  // *ends* -- which, for a live ingestion timeline or a streaming answer, is
  // precisely the moment the information stops being useful.
  const streaming =
    contentType.includes("text/event-stream") || upstream.headers.has("content-disposition");

  const headers = new Headers();
  if (contentType) headers.set("content-type", contentType);
  const disposition = upstream.headers.get("content-disposition");
  if (disposition) headers.set("content-disposition", disposition);

  if (streaming && upstream.body) {
    headers.set("cache-control", "no-cache, no-transform");
    headers.set("x-accel-buffering", "no");
    return new NextResponse(upstream.body, { status: upstream.status, headers });
  }

  return new NextResponse(await upstream.arrayBuffer(), {
    status: upstream.status,
    headers,
  });
}
