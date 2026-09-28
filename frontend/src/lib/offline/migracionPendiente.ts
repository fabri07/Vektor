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
    // B13 no tenía `charged_at`, y sin él el servidor rechaza con 422
    // `OFFLINE_WITHOUT_CHARGED_AT` **siempre** que la venta llegue por la cola
    // —alcance el stock o no—: migrarla sin esto la metía en una cola que no
    // podía subirla nunca. No se inventa una fecha: B13 congelaba todo el
    // payload al apretar Cobrar, así que su `operation_date` ES el momento del
    // cobro, el mismo valor que B7 escribe hoy en los dos campos.
    const conCobro: PosOperationPayload = payload.charged_at
      ? payload
      : { ...payload, charged_at: payload.operation_date };
    await guardarOperacion(
      {
        clientOperationId: conCobro.client_operation_id,
        tenantId: datos.tenantId,
        userId: datos.userId,
        terminalId: datos.terminalId,
        payload: conCobro,
        lines,
        recibo: reciboLocal(conCobro, lines, {
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
    return conCobro.client_operation_id;
  } catch {
    // Un JSON roto no puede impedir que la caja abra. La clave queda: si más
    // adelante se puede leer, se migra; mientras tanto no se pierde el dato.
    return null;
  }
}
