import { Suspense } from "react";
import type { Metadata } from "next";

import { PublicNav } from "@/components/public/PublicNav";
import { PublicFooter } from "@/components/public/PublicFooter";
import { PageHeader } from "@/components/public/PageHeader";
import { AccessRequestForm } from "@/features/access-request/AccessRequestForm";

export const metadata: Metadata = {
  title: "Pedir acceso | Véktor",
  description:
    "Contanos de tu negocio y pedí acceso a Véktor. Revisamos cada solicitud a mano.",
};

/**
 * `/solicitar-acceso` — la puerta de entrada pública desde que se cerró el
 * registro abierto. `/register` redirige acá.
 *
 * Vive en `(public)` (sin el layout de marketing) pero monta el nav y el footer
 * públicos a mano: el visitante llega desde /precios o desde la landing y tiene
 * que poder seguir navegando el sitio si se arrepiente a mitad del formulario.
 */
export default function SolicitarAccesoPage() {
  return (
    <div className="min-h-screen bg-vektor-night">
      <PublicNav />
      <main className="pt-16">
        <PageHeader
          eyebrow="Acceso por solicitud"
          title="Pedí acceso a Véktor"
          subtitle={
            <>
              Revisamos cada solicitud a mano: queremos entender tu negocio antes de
              darte una cuenta, para asegurarnos de que podemos ayudarte. Te lleva un
              minuto.
            </>
          }
        />
        <Suspense>
          <AccessRequestForm />
        </Suspense>
      </main>
      <PublicFooter />
    </div>
  );
}
