import { NextRequest, NextResponse } from "next/server";

/**
 * Keep signed-out visitors off console pages.
 *
 * This is a redirect for the user's benefit, not a security boundary -- the
 * cookie is only checked for presence here. Every actual authorisation decision
 * is the backend's, on a token it verifies itself.
 */
export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const console_ = pathname.startsWith("/owner") ? "owner" : "admin";

  if (pathname === `/${console_}/login`) return NextResponse.next();
  if (request.cookies.get(`beacon_${console_}_at`)) return NextResponse.next();

  const url = request.nextUrl.clone();
  url.pathname = `/${console_}/login`;
  return NextResponse.redirect(url);
}

export const config = { matcher: ["/owner/:path*", "/admin/:path*"] };
