import Link from "next/link";
import { PageWrapper } from "@/components/layout/PageWrapper";

/**
 * Portada de la consola de la plataforma. Por ahora son accesos directos; el
 * monitor con datos de toda la plataforma lo reemplaza en el PR siguiente.
 */
const ACCESOS = [
  {
    href: "/admin/tenants",
    titulo: "Negocios",
    detalle: "Estado de cuenta, suscripción, pagos y uso de cada negocio.",
  },
  {
    href: "/admin/usage",
    titulo: "Uso & costos",
    detalle: "Consumo de tokens y costo estimado de la plataforma.",
  },
];

export default function AdminIndexPage() {
  return (
    <PageWrapper title="Consola de la plataforma">
      <div className="grid gap-4 sm:grid-cols-2">
        {ACCESOS.map((a) => (
          <Link
            key={a.href}
            href={a.href}
            className="rounded-lg border border-vk-border bg-vk-surface-w p-5 transition-colors hover:border-vk-blue/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-vk-blue/60"
          >
            <h2 className="text-base font-semibold text-vk-text-primary">{a.titulo}</h2>
            <p className="mt-1 text-sm text-vk-text-muted">{a.detalle}</p>
          </Link>
        ))}
      </div>
    </PageWrapper>
  );
}
