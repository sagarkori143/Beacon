import { NextRequest, NextResponse } from "next/server";

import { API_BASE, LOGIN_PATH, type Console, clearSession, setSession } from "./session";

/**
 * Exchange credentials for a session cookie.
 *
 * The tokens are set server-side and never returned to the page, so nothing the
 * browser can read ever holds a credential.
 */
export async function login(request: NextRequest, c: Console): Promise<NextResponse> {
  const credentials = await request.json();

  const upstream = await fetch(`${API_BASE}${LOGIN_PATH[c]}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(credentials),
    cache: "no-store",
  });

  const payload = await upstream.json().catch(() => ({}));
  if (!upstream.ok) {
    return NextResponse.json(payload, { status: upstream.status });
  }

  // The tenant API happily issues a token to a plain USER, who would then be
  // refused by every screen in this console. Say so at the door instead.
  if (c === "admin" && payload.role !== "ADMIN") {
    return NextResponse.json(
      {
        type: "about:blank#forbidden",
        title: "not an administrator",
        status: 403,
        detail:
          "This console is for organization administrators. Your account is a regular user.",
      },
      { status: 403 },
    );
  }

  await setSession(c, payload);
  return NextResponse.json({
    ok: true,
    organization: payload.organization ?? null,
    email: payload.email ?? null,
    role: payload.role ?? null,
  });
}

export async function logout(c: Console): Promise<NextResponse> {
  await clearSession(c);
  return NextResponse.json({ ok: true });
}
