import axios from "axios";

/**
 * Lo que la caja necesita de un error del servidor, sin importar su forma.
 *
 * Hay dos formas en el backend: `HTTPException(detail={code, ...})` (el código
 * adentro de `detail`) y el handler global (`detail` es texto y `code` va al
 * lado, p. ej. `INSUFFICIENT_STOCK`). Espejo de `errorCode()` de
 * `useOfflineSubmit.ts`, pero además conserva el resto de los campos.
 */
export interface PosErrorInfo {
  /** `null` = no hubo respuesta (ver `tipo` para saber por qué). */
  status: number | null;
  code: string | null;
  message: string | null;
  /** Los campos del error (del `detail` si era objeto, si no del cuerpo). */
  fields: Record<string, unknown>;
  /** Qué clase de fallo fue. Ver `TipoDeFallo`. */
  tipo: TipoDeFallo;
  /** La correlación con los logs y Sentry del backend, si se conoce. */
  traceId: string | null;
}

/**
 * Qué pasó, sin inventar la causa. Hasta 2026-10 todo lo que no traía
 * respuesta se llamaba "Sin conexión": un preflight de CORS rechazado, un 500
 * sin CORS y una excepción de JS le decían al cajero que revisara el router.
 *
 * - `respuesta`: el servidor contestó (hay `status`).
 * - `timeout`: se esperó y no contestó a tiempo. Pudo haber entrado.
 * - `transporte`: el navegador no entregó una respuesta. Red caída, DNS o CORS:
 *   desde JS no se pueden distinguir, así que no se afirma ninguna.
 * - `cancelada`: la abortó la propia app. No es un fallo.
 * - `local`: ni siquiera fue un error HTTP: es un error del código de la caja.
 *   Reintentar no lo arregla.
 */
export type TipoDeFallo = "respuesta" | "timeout" | "transporte" | "cancelada" | "local";

function traceIdDe(e: { config?: { headers?: unknown }; response?: { headers?: unknown } }): string | null {
  const leer = (h: unknown, nombre: string): string | null => {
    if (!h || typeof h !== "object") return null;
    const v = (h as Record<string, unknown>)[nombre];
    return typeof v === "string" && v ? v : null;
  };
  return (
    leer(e.response?.headers, "x-trace-id") ??
    leer(e.config?.headers, "X-Trace-Id") ??
    null
  );
}

export function posErrorInfo(e: unknown): PosErrorInfo {
  if (!axios.isAxiosError(e)) {
    return { status: null, code: null, message: null, fields: {}, tipo: "local", traceId: null };
  }
  const traceId = traceIdDe(e);
  if (!e.response) {
    const tipo: TipoDeFallo = axios.isCancel(e)
      ? "cancelada"
      : e.code === "ECONNABORTED" || e.code === "ETIMEDOUT"
        ? "timeout"
        : "transporte";
    return { status: null, code: null, message: null, fields: {}, tipo, traceId };
  }
  const data = (e.response.data ?? {}) as Record<string, unknown>;
  const detail = data.detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as Record<string, unknown>;
    return {
      status: e.response.status,
      code: typeof d.code === "string" ? d.code : null,
      message: typeof d.message === "string" ? d.message : null,
      fields: d,
      tipo: "respuesta",
      traceId,
    };
  }
  return {
    status: e.response.status,
    code: typeof data.code === "string" ? data.code : typeof detail === "string" ? detail : null,
    message: typeof detail === "string" ? detail : null,
    fields: data,
    tipo: "respuesta",
    traceId,
  };
}

/**
 * El motivo de un envío que no tuvo respuesta, en palabras del cajero.
 *
 * "Sin conexión" sólo cuando el navegador dice que está desconectado: con
 * `navigator.onLine === true` la red de la PC anda, y lo honesto es decir que
 * el servidor no contestó, sin adivinar si fue la red de afuera o el servidor.
 */
export function describirFalloSinRespuesta(info: PosErrorInfo): string {
  if (typeof navigator !== "undefined" && navigator.onLine === false) {
    return "Sin conexión";
  }
  switch (info.tipo) {
    case "timeout":
      return "El servidor no respondió a tiempo";
    case "local":
      return "Error interno de la caja";
    default:
      return "No se pudo contactar al servidor";
  }
}

/** Referencia corta para dictar por teléfono o buscar en Sentry. */
export function referenciaCorta(traceId: string | null | undefined): string | null {
  return traceId ? traceId.slice(0, 8) : null;
}

/** El texto para el cajero de un cobro que el servidor rechazó. */
export function cobroRejectionMessage(info: PosErrorInfo): string {
  switch (info.code) {
    case "INSUFFICIENT_STOCK":
      return info.message ?? "No hay stock suficiente de un producto.";
    case "PRODUCT_WITHOUT_PRICE":
      return `${info.message ?? "Un producto no tiene precio."} Pedile al encargado que lo cargue.`;
    case "DISCOUNT_EXCEEDS_TOTAL":
      return "El descuento supera el importe. Revisalo.";
    case "CASH_RECEIVED_TOO_LOW":
      return "El efectivo recibido no alcanza para la parte en efectivo.";
    case "CASH_RECEIVED_WITHOUT_CASH_TENDER":
      return "Se informó efectivo recibido pero ningún pago es en efectivo. Revisá los pagos.";
    case "TENDERS_MISMATCH":
      return "Los pagos no suman el total de la venta. Revisá el reparto y volvé a cobrar.";
    case "TERMINAL_REQUIRED":
      return "Esta PC no está habilitada como caja. Pedile al dueño que la habilite en Configuración → Seguridad.";
    case "TERMINAL_DISABLED":
      return "Esta caja fue dada de baja. Pedile al dueño que la vuelva a habilitar.";
    case "TERMINAL_UNKNOWN":
      return "Esta PC no es una caja de este negocio. Pedile al dueño que la habilite.";
    case "IDEMPOTENCY_KEY_REUSED":
      return "El servidor ya tiene otra venta con este número. No cobres de nuevo: avisale al encargado.";
    default:
      break;
  }
  if (info.status === 402) {
    // Desde B14 la caja no tiene gate de suscripción: un 402 acá no debería
    // llegar. Si llega, no se afirma nada sobre la venta.
    return "El servidor rechazó el cobro por la suscripción del negocio. Avisale al dueño.";
  }
  if (info.status === 403) {
    return info.message ?? "Tu usuario no tiene permiso para esta operación.";
  }
  return info.message ?? "El servidor rechazó el cobro.";
}
