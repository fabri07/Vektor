"use client";

import type { ColumnMappingSuggestion } from "@/services/ingestion.service";

/**
 * El estado de una columna, en un punto de color.
 *
 * Extraído del panel junto con `AmbiguityHint`: los dos describen la MISMA
 * columna y se dibujan siempre juntos, así que separarlos en archivos distintos
 * del render los mantiene alineados.
 */
export function StatusDot({ status }: { status: ColumnMappingSuggestion["status"] }) {
  if (status === "mapped") {
    return <span className="h-2 w-2 rounded-full bg-vk-success shrink-0" title="Mapeado" />;
  }
  // F-M: entender la columna y no poder decidir NO es lo mismo que no
  // entenderla. Si cayera al punto de "Sin mapear" de abajo, la pantalla
  // borraría justamente la distinción que el backend calcula.
  if (status === "ambiguo") {
    return (
      <span
        className="h-2 w-2 rounded-full bg-vk-blue shrink-0"
        title="Necesita que elijas entre dos lecturas"
      />
    );
  }
  if (status === "required_missing") {
    return (
      <span
        className="h-2 w-2 rounded-full bg-vk-danger shrink-0"
        title="Campo requerido faltante"
      />
    );
  }
  return <span className="h-2 w-2 rounded-full bg-vk-warning shrink-0" title="Sin mapear" />;
}
