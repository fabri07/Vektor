import { redirect } from "next/navigation";

/** El resumen económico ahora vive como pestaña Balance del dashboard. */
export default function ResumenEconomicoRedirect() {
  redirect("/dashboard/balance");
}
