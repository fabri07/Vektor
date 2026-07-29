import { Suspense } from "react";
import type { Metadata } from "next";

import { FunnelSplitShell } from "@/components/public/FunnelSplitShell";
import { SolicitudEnviada } from "./SolicitudEnviadaContenido";

/**
 * `/solicitud-enviada` — pantalla posterior al envío del formulario.
 *
 * Es Server Component para poder declarar su propia `metadata`: con el
 * `"use client"` que tenía en la línea 1, Next lo prohíbe y la pantalla
 * heredaba el `<title>` genérico del layout raíz. El estado vive en
 * `SolicitudEnviadaContenido`, que sí es cliente.
 */
export const metadata: Metadata = {
  title: "Revisá tu correo | Véktor",
  description:
    "Te mandamos un link para confirmar tu email. Tu solicitud entra a revisión cuando lo confirmes.",
};

export default function SolicitudEnviadaPage() {
  return (
    <FunnelSplitShell aside="Un paso más y tu solicitud entra a revisión.">
      <Suspense>
        <SolicitudEnviada />
      </Suspense>
    </FunnelSplitShell>
  );
}
