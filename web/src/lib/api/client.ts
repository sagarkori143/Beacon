"use client";

/** A problem document as the backend sends it (RFC 9457). */
export type Problem = {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  errors?: unknown;
  request_id?: string;
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly problem: Problem;

  constructor(status: number, problem: Problem) {
    super(problem.detail || problem.title || `Request failed (${status})`);
    this.status = status;
    this.problem = problem;
    // "about:blank#tenant_scope_violation" -> "tenant_scope_violation"
    this.code = problem.type?.split("#")[1] ?? `http_${status}`;
  }
}

/**
 * Call the backend through this console's proxy.
 *
 * `path` is relative to the console's API root -- "organizations" for the owner
 * console, "users" for the admin one -- because the proxy decides which backend
 * prefix and which credential to use, not the page.
 */
export async function api<T>(
  console_: "owner" | "admin",
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api/${console_}/${path}`, {
    ...init,
    headers: {
      ...(init.body ? { "content-type": "application/json" } : {}),
      ...init.headers,
    },
  });

  if (!response.ok) {
    const problem = (await response.json().catch(() => ({}))) as Problem;
    throw new ApiError(response.status, problem);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export type Page<T> = {
  items: T[];
  total: number | null;
  limit: number;
  offset: number;
  has_more: boolean;
};

export type TenantUser = {
  id: string;
  email: string;
  full_name: string | null;
  role: "USER" | "ADMIN";
  location_id: string | null;
  is_active: boolean;
};

export type Organization = {
  id: string;
  name: string;
  slug: string;
  is_active: boolean;
  /** Listed on the public site and answering questions from visitors. */
  is_public: boolean;
  created_at: string;
};
