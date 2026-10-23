import { Suspense } from "react";
import type { Metadata } from "next";

import { PublicNav } from "@/components/public/PublicNav";
import { PublicFooter } from "@/components/public/PublicFooter";
import { PageHeader } from "@/components/public/PageHeader";
import { AccessRequestForm } from "@/features/access-request/AccessRequestForm";
// El número de preguntas sale del schema, no de un literal: escrito a mano se
// desincroniza en cuanto alguien agrega o saca un campo, y ya había cuatro
// cifras distintas dando vueltas por el repo.
import { REQUIRED_FIELD_COUNT } from "@/validation/accessRequest";

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
          eyebrow="Primer paso"
          title="Contanos cómo se mueve tu negocio"
          subtitle={
            <>
              Revisamos cada solicitud para confirmar que Véktor pueda darte
              una lectura útil desde el inicio. Son {REQUIRED_FIELD_COUNT}{" "}
              respuestas breves y suele llevar menos de tres minutos.
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
