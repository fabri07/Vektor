import { redirect } from "next/navigation";

/** `/admin` no tiene pantalla propia: la entrada de la plataforma es el monitor de negocios. */
export default function AdminIndexPage() {
  redirect("/admin/tenants");
}
