import type { ReactNode } from "react";

import { VektorLogo } from "@/components/ui/VektorLogo";

/**
 * El armazón de dos columnas de las pantallas finales del embudo de acceso:
 * `/solicitud-enviada`, `/solicitud-verificada`, `/definir-password` y
 * `/login`.
 *
 * Existe porque había cuatro copias del mismo bloque, ya divergiendo en
 * detalles accidentales: `max-w-[420px]` en dos y `max-w-[400px]` en las
 * otras, `md:overflow-y-auto` presente en dos y ausente en la tercera.
 * Ninguna de esas diferencias era una decisión — eran copias que se fueron
 * separando.
 *
 * **No es un componente cliente.** Las páginas que lo usan pueden quedarse
 * como Server Components y exportar su propia `metadata`, que era justamente
 * lo que no podían hacer con el `"use client"` en la línea 1.
 */
export function FunnelSplitShell({
  aside,
  asideExtra,
  children,
}: {
  /** Una línea de contexto para el panel izquierdo: dónde está el visitante. */
  aside: ReactNode;
  /**
   * Contenido adicional del panel izquierdo. Solo `/login` lo usa hoy (lista
   * de beneficios, collage y banda de confianza).
   *
   * Es opcional y no un default para todas: llenar el panel de las tres
   * pantallas de espera es una decisión de diseño pendiente, no un fix
   * técnico. Lo que este componente resuelve es que sean UNA implementación,
   * para que esa decisión después toque un archivo y no cuatro.
   */
  asideExtra?: ReactNode;
  children: ReactNode;
}) {
  return (
    <main className="min-h-screen md:grid md:grid-cols-2">
      {/* Panel de marca. Oculto en móvil: ahí el logo va arriba del contenido. */}
      <div className="hidden flex-col bg-vk-bg-dark px-12 py-12 md:flex">
        <div>
          <VektorLogo variant="full" size="lg" theme="dark" />
          <p className="mt-3 text-base text-vk-text-muted">{aside}</p>
        </div>
        {asideExtra}
      </div>

      <div className="flex min-h-screen items-center justify-center bg-vk-surface-w px-8 py-12 md:min-h-0 md:overflow-y-auto">
        <div className="w-full max-w-[420px]">
          <div className="mb-8 md:hidden">
            <VektorLogo variant="full" size="md" theme="light" />
          </div>
          {children}
        </div>
      </div>
    </main>
  );
}
