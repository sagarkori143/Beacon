import { cookies } from "next/headers";

/**
 * The two credential kinds, kept apart everywhere.
 *
 * A platform token is rejected by every tenant endpoint and a tenant token by
 * every platform endpoint -- the backend enforces that in its token decoder,
 * before any permission check. The frontend mirrors the split rather than
 * papering over it: two cookie pairs, two proxies, two login pages. Nothing
 * here is parameterised by "which console am I", because that parameter is
 * exactly the thing that must never become a runtime value.
 */
export type Console = "owner" | "admin";

export const API_BASE =
  process.env.API_INTERNAL_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

/** Where each console's calls land on the backend. */
export const API_PREFIX: Record<Console, string> = {
  owner: "/api/v1/platform",
  admin: "/api/v1",
};

export const LOGIN_PATH: Record<Console, string> = {
  owner: "/api/v1/platform/auth/login",
  admin: "/api/v1/auth/login",
};

export const REFRESH_PATH: Record<Console, string> = {
  owner: "/api/v1/platform/auth/refresh",
  admin: "/api/v1/auth/refresh",
};

export function cookieNames(c: Console) {
  return { access: `beacon_${c}_at`, refresh: `beacon_${c}_rt` };
}

/**
 * Tokens live in httpOnly cookies rather than localStorage.
 *
 * An admin token grants uploads, user creation and disable across a whole
 * tenant, and a refresh token is good for fourteen days. In localStorage that
 * is one XSS away from a two-week tenant compromise; in an httpOnly cookie no
 * page script can read it at all.
 */
export async function setSession(
  c: Console,
  tokens: { access_token: string; refresh_token: string; expires_in?: number },
) {
  const jar = await cookies();
  const names = cookieNames(c);
  const base = {
    httpOnly: true,
    sameSite: "lax" as const,
    secure: process.env.NODE_ENV === "production",
    path: "/",
  };
  jar.set(names.access, tokens.access_token, {
    ...base,
    maxAge: tokens.expires_in ?? 1800,
  });
  jar.set(names.refresh, tokens.refresh_token, { ...base, maxAge: 60 * 60 * 24 * 14 });
}

export async function clearSession(c: Console) {
  const jar = await cookies();
  const names = cookieNames(c);
  jar.delete(names.access);
  jar.delete(names.refresh);
}

export async function readSession(c: Console) {
  const jar = await cookies();
  const names = cookieNames(c);
  return {
    access: jar.get(names.access)?.value ?? null,
    refresh: jar.get(names.refresh)?.value ?? null,
  };
}
