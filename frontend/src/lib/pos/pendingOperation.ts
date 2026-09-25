import type { CartLine, PosOperationPayload } from "@/lib/pos/cart";

/**
 * El cobro que quedó en duda, guardado hasta resolverse (B13).
 *
 * Sin esto, recargar la página con un cobro sin respuesta borraba el carrito y
 * el cajero lo volvía a cobrar: si el primero había entrado, doble cobro. Se
 * escribe ANTES de mandar (el mismo principio que B7 va a llevar a IndexedDB) y
 * se borra sólo cuando el servidor contesta algo definitivo.
 *
 * Clave propia: `authStore.logout()` no la toca, igual que la terminal (B6).
 * Todo con try/catch: sin localStorage (modo privado, cuota llena) la caja
 * sigue funcionando, sólo pierde este resguardo.
 */

const KEY = "vektor-pos-pending-operation";

export interface PendingOperation {
  tenantId: string;
  payload: PosOperationPayload;
  /** Para volver a mostrar el carrito tal como se cobró. */
  lines: CartLine[];
  savedAt: string;
}

export function savePending(p: PendingOperation): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(p));
  } catch {
    // Ver el comentario del módulo.
  }
}

export function loadPending(tenantId: string): PendingOperation | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as PendingOperation;
    // Una venta de OTRO negocio no se reintenta con la sesión de éste.
    if (p.tenantId !== tenantId || !p.payload?.client_operation_id) return null;
    return p;
  } catch {
    return null;
  }
}

export function clearPending(clientOperationId: string): void {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return;
    const p = JSON.parse(raw) as PendingOperation;
    // Sólo la que se resolvió: nunca pisar otra que no es la de este cobro.
    if (p.payload?.client_operation_id === clientOperationId) {
      window.localStorage.removeItem(KEY);
    }
  } catch {
    // Ver el comentario del módulo.
  }
}
