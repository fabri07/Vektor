/**
 * Puente de una sola vez: el resguardo de B13 pasa a la cola de B7.
 *
 * B13 guardaba la venta "en duda" en `localStorage`, una sola operación, con la
 * clave `vektor-pos-pending-operation`. Al desplegar B7 puede haber una PC con
 * una venta ahí: cobrada, sin confirmar y sin forma de subirla si esta función
 * no existiera. Se mueve a IndexedDB y se borra la clave vieja, así la
 * migración no se repite.
 *
 * Lee la clave a mano en vez de reusar el módulo viejo porque ese módulo se
 * elimina: conservarlo sólo para esto dejaría dos lugares que saben dónde vive
 * un pendiente.
 */

import { guardarOperacion } from "@/lib/offline/posDb";
import type { CartLine, PosOperationPayload } from "@/lib/pos/cart";
import { reciboLocal } from "@/lib/pos/reciboLocal";

const CLAVE_VIEJA = "vektor-pos-pending-operation";

export interface DatosDeMigracion {
  tenantId: string;
  userId: string;
  terminalId: string | null;
  businessName: string;
  cashierName: string | null;
  terminalName: string | null;
}

/** Devuelve el `client_operation_id` migrado, o `null` si no había nada. */
export async function migrarPendienteDeB13(
  datos: DatosDeMigracion,
): Promise<string | null> {
  let crudo: string | null = null;
  try {
    crudo = window.localStorage.getItem(CLAVE_VIEJA);
  } catch {
    return null;
  }
  if (!crudo) return null;

  try {
    const viejo = JSON.parse(crudo) as {
      tenantId?: string;
      payload?: PosOperationPayload;
      lines?: CartLine[];
    };
    const payload = viejo.payload;
    // De otro negocio **no se toca, ni para borrarla**: es un cobro que espera
    // a que entre la sesión de ese negocio. Borrarla acá —como se hacía— tira
    // una venta ajena en una PC compartida por dos locales.
    if (payload?.client_operation_id && viejo.tenantId !== datos.tenantId) return null;
    if (!payload?.client_operation_id) {
      // Sin id no hay nada que migrar y nada que perder: la clave se limpia.
      window.localStorage.removeItem(CLAVE_VIEJA);
      return null;
    }
    const lines = viejo.lines ?? [];
    // **El payload NO se toca acá.** Agregarle `charged_at` cambia la huella de
    // idempotencia (`hash_peticion` usa `exclude_defaults=True`), y si el primer
    // envío SÍ había llegado al servidor y sólo se perdió la respuesta —que es
    // exactamente el caso para el que existe este resguardo—, el reintento
    // recibiría 409 `IDEMPOTENCY_KEY_REUSED` en vez del ticket original. Se
    // guarda tal cual y se marca de dónde viene: `usePosSync` reclama primero
    // con el contenido original y sólo lo adapta si el servidor confirma que la
    // venta no existe.
    await guardarOperacion(
      {
        clientOperationId: payload.client_operation_id,
        origen: "b13",
        tenantId: datos.tenantId,
        userId: datos.userId,
        terminalId: datos.terminalId,
        payload,
        lines,
        recibo: reciboLocal(payload, lines, {
          businessName: datos.businessName,
          cashierName: datos.cashierName,
          terminalName: datos.terminalName,
          customerName: null,
        }),
      },
      // Su primer envío ya pasó: la cola puede tomarla en el acto.
      { sinPrimerEnvio: true },
    );
    window.localStorage.removeItem(CLAVE_VIEJA);
    return payload.client_operation_id;
  } catch {
    // Un JSON roto no puede impedir que la caja abra. La clave queda: si más
    // adelante se puede leer, se migra; mientras tanto no se pierde el dato.
    return null;
  }
}
