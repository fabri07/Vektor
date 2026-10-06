/**
 * Compuerta de relectura (B1, 2026-10-06).
 *
 * Dos números distintos, y no hay que confundirlos:
 *
 * - El PREVIEW estima qué se reemplaza y qué se pierde. Es informativo: reconstruye
 *   lo que haría el import y puede errar (la revisión lo midió en los dos sentidos).
 * - El APPLY mide de verdad, dentro de su transacción. Si algo se pierde y no se
 *   aceptó, no guarda nada: el run vuelve con `error_code = REREAD_WOULD_LOSE_DATA`
 *   y `data_loss`, y aplicar igual es reenviar el apply con `accept_data_loss`.
 *
 * Un backend viejo no manda `to_lose` en el preview: ahí no hay nada que mostrar.
 */

import type {
  RereadCounts,
  RereadDataLoss,
  RereadRunStatusResponse,
} from "@/services/ingestion.service";

export const REREAD_WOULD_LOSE_DATA = "REREAD_WOULD_LOSE_DATA";

/** `true` si el backend ya separa reemplazo de pérdida en el preview. */
export function midePerdidas(counts: RereadCounts): boolean {
  return typeof counts.to_lose === "number";
}

/** La medición exacta si el apply se frenó porque perdería datos. */
export function perdidaDelApply(data: RereadRunStatusResponse): RereadDataLoss | null {
  if (data.error_code === REREAD_WOULD_LOSE_DATA && data.data_loss) {
    return data.data_loss;
  }
  return null;
}
