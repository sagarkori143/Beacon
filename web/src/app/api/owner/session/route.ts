import { NextRequest } from "next/server";

import { login, logout } from "@/lib/api/login";

export const POST = (request: NextRequest) => login(request, "owner");
export const DELETE = () => logout("owner");
