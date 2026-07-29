import { Suspense } from "react";
import type { Metadata } from "next";

import { FunnelSplitShell } from "@/components/public/FunnelSplitShell";
import { SolicitudVerificada } from "./SolicitudVerificadaContenido";

/**
 * `/solicitud-verificada?token=` — confirma el email de una solicitud.
 *
 * Server Component por la `metadata`; el POST del token y su estado viven en
 * `SolicitudVerificadaContenido`.
 */
export const metadata: Metadata = {
  title: "Email confirmado | Véktor",
  description: "Tu solicitud de acceso entró a la cola de revisión.",
};

export default function SolicitudVerificadaPage() {
  return (
    <FunnelSplitShell aside="Tu solicitud está en revisión. Te escribimos por mail.">
      <Suspense>
        <SolicitudVerificada />
      </Suspense>
    </FunnelSplitShell>
  );
}
