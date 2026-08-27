import type { ErrorEvent, EventHint } from "@sentry/nextjs";

/**
 * `sendDefaultPii: false` evita que el SDK agregue PII automáticamente, pero
 * no filtra lo que el código de Véktor agrega explícitamente: el `config`
 * completo de un `AxiosError` capturado (headers de auth, body de la
 * request), breadcrumbs con datos de negocio, o `extra` con montos/CUIT/DNI
 * cargados en pantalla. Este scrubbing es una capa aparte, compartida por
 * `instrumentation-client.ts` (browser) e `instrumentation.ts` (server).
 */

const REDACTED = "[Filtered]";

const SENSITIVE_KEY_RE =
  /(authoriz|token|password|cookie|secret|cuit|dni|email|phone|telefono|amount|monto|customer_name|supplier_name|nombre_cliente|nombre_proveedor)/i;

const CUIT_VALUE_RE = /\b\d{2}-?\d{8}-?\d\b/;
const DNI_VALUE_RE = /\b\d{7,8}\b/;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function redactValue(key: string, value: unknown): unknown {
  if (SENSITIVE_KEY_RE.test(key)) return REDACTED;
  if (typeof value === "string" && (CUIT_VALUE_RE.test(value) || DNI_VALUE_RE.test(value))) {
    return REDACTED;
  }
  return value;
}

function scrubMapping(data: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(data)) {
    result[key] = redactValue(key, value);
  }
  return result;
}

interface AxiosLikeError {
  isAxiosError?: boolean;
  config?: {
    headers?: unknown;
    data?: unknown;
  };
}

/**
 * `beforeSend` compartido. Redacta, en orden:
 * 1. Headers/query string del `request` del evento.
 * 2. `config.headers`/`config.data` de un `AxiosError` capturado — ahí vive
 *    el `Authorization` y el body serializado de la request que falló.
 * 3. `extra` y `breadcrumbs[*].data` por el mismo criterio de claves/valores
 *    que el `_scrub_event` del backend (mismo vocabulario de negocio).
 */
export function scrubSentryEvent(event: ErrorEvent, hint: EventHint): ErrorEvent | null {
  if (event.request) {
    if (isPlainObject(event.request.headers)) {
      event.request.headers = scrubMapping(event.request.headers) as typeof event.request.headers;
    }
    if (typeof event.request.query_string === "string" && SENSITIVE_KEY_RE.test(event.request.query_string)) {
      event.request.query_string = REDACTED;
    }
  }

  // `hint.originalException` es el AxiosError real que el resto de la app
  // todavía puede tener referenciado (ej. el propio `catch` que llamó a
  // `captureException`) — nunca mutar `config` in place, o la reescritura de
  // este beforeSend se filtraría al código de la app. Se arma una copia
  // saneada y se adjunta como contexto del evento.
  const original = hint.originalException as AxiosLikeError | undefined;
  if (original?.isAxiosError && original.config) {
    const { config } = original;
    const axiosContext: Record<string, unknown> = {};
    if (isPlainObject(config.headers)) {
      axiosContext.headers = scrubMapping(config.headers);
    }
    if (config.data !== undefined) {
      axiosContext.data = REDACTED;
    }
    if (Object.keys(axiosContext).length > 0) {
      event.contexts = { ...event.contexts, axios_request: axiosContext };
    }
  }

  if (isPlainObject(event.extra)) {
    event.extra = scrubMapping(event.extra);
  }

  for (const crumb of event.breadcrumbs ?? []) {
    if (isPlainObject(crumb.data)) {
      crumb.data = scrubMapping(crumb.data);
    }
  }

  return event;
}
