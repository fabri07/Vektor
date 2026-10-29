/**
 * Qué hacer con una venta pendiente según lo que contestó el servidor.
 *
 * UNA sola clasificación, por CÓDIGO y no por rango. `isDefinitiveRejection`
 * (`cart.ts`) sirve para el primer envío sincrónico —donde el carrito sigue
 * bloqueado y todavía no hubo entrega— pero no para la cola: trata como
 * definitivos el 402 (suscripción vencida) y el 423 (mantenimiento), que son
 * exactamente los dos que se arreglan solos cuando el dueño paga o el
 * mantenimiento termina.
 *
 * **Una venta que ya se cobró no se borra por ninguna respuesta del servidor.**
 * A lo sumo cambia de estado y queda a la vista. El único borrado posterior es
 * un descarte manual con confirmación explícita.
 */

import type { PosErrorInfo } from "@/lib/pos/errors";

export type ClaseSync =
  /** Entró: el servidor la tiene. */
  | "sincronizada"
  /** No se pudo saber, o se puede volver a intentar solo. */
  | "reintento"
  /** Hace falta volver a entrar, o habilitar de nuevo esta PC. */
  | "autenticacion"
  /** El servidor la rechazó: la mira una persona. */
  | "revision";

/** Códigos que dicen "esta PC o esta sesión ya no sirven", no "la venta está mal". */
const DE_AUTENTICACION = new Set([
  "TERMINAL_DISABLED",
  "TERMINAL_UNKNOWN",
  "TERMINAL_REQUIRED",
  "TERMINAL_REQUIRED_FOR_CAPTURE",
]);

/** Se arreglan sin tocar la venta: el dueño paga, o el mantenimiento termina. */
const REINTENTABLES = new Set([402, 408, 423, 429, 503]);

/**
 * Cuenta suspendida o en solo lectura (panel SUPERADMIN): también se arregla sin
 * tocar la venta, cuando el soporte reactiva. La venta queda en la cola, con el
 * motivo a la vista, y entra sola al reactivar. Son 403, y un 403 de otro tipo
 * sigue siendo "la mira una persona".
 */
const REINTENTABLES_POR_CODIGO = new Set(["TENANT_SUSPENDED", "TENANT_READ_ONLY"]);

export function clasificarSync(info: PosErrorInfo): ClaseSync {
  const { status } = info;
  // Un error del código de la caja no se arregla reintentando: se repetiría
  // igual cada 5 segundos para siempre, con la venta "pendiente" a la vista y
  // sin que nadie se entere. Lo mira una persona (y queda en Sentry).
  if (info.tipo === "local") return "revision";
  // Sin respuesta: la venta pudo haber entrado. Reintentar es seguro porque el
  // `client_operation_id` hace que el servidor devuelva el ticket original.
  if (status === null) return "reintento";
  if (status >= 200 && status < 300) return "sincronizada";
  if (status >= 500 || REINTENTABLES.has(status)) return "reintento";
  if (status === 403 && info.code && REINTENTABLES_POR_CODIGO.has(info.code)) {
    return "reintento";
  }
  if (status === 401) return "autenticacion";
  if (status === 403 && info.code && DE_AUTENTICACION.has(info.code)) {
    return "autenticacion";
  }
  return "revision";
}

/**
 * Sin respuesta del servidor (red, DNS, CORS, timeout): no gasta presupuesto de
 * reintentos (lección de B0). Una excepción local NO cuenta: no es la red.
 */
export function esFalloDeRed(info: PosErrorInfo): boolean {
  return info.status === null && info.tipo !== "local";
}
