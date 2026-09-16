import { NextRequest } from "next/server";

import { login, logout } from "@/lib/api/login";

export const POST = (request: NextRequest) => login(request, "admin");
export const DELETE = () => logout("admin");
