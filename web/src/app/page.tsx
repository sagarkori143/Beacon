import { redirect } from "next/navigation";

import { readSession } from "@/lib/api/session";

export default async function Home() {
  // Land wherever the browser already holds a credential; otherwise pick the
  // console most people arrive for.
  if ((await readSession("admin")).access) redirect("/admin");
  if ((await readSession("owner")).access) redirect("/owner");
  redirect("/admin/login");
}
