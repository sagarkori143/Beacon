import { NextRequest } from "next/server";

import { proxy } from "@/lib/api/proxy";

type Ctx = { params: Promise<{ path: string[] }> };

const handler = async (request: NextRequest, ctx: Ctx) =>
  proxy(request, "admin", (await ctx.params).path);

export const GET = handler;
export const POST = handler;
export const PATCH = handler;
export const PUT = handler;
export const DELETE = handler;
