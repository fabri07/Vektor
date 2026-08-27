import * as Sentry from "@sentry/nextjs";
import { scrubSentryEvent } from "@/lib/sentryScrub";

export function register(): void {
  // DSN server-side, separado del NEXT_PUBLIC_SENTRY_DSN del browser — permite
  // apagar un lado (ej. Replay/cliente) sin perder captura server-side, y
  // evita que un proceso de servidor dependa de una variable pensada para el
  // navegador.
  const dsn = process.env.SENTRY_DSN;
  if (!dsn) {
    if (process.env.NODE_ENV === "development") {
      // eslint-disable-next-line no-console
      console.info("[sentry] deshabilitado (server): SENTRY_DSN vacío");
    }
    return;
  }

  if (process.env.NEXT_RUNTIME === "nodejs" || process.env.NEXT_RUNTIME === "edge") {
    Sentry.init({
      dsn,
      environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT,
      sendDefaultPii: false,
      sampleRate: 1.0,
      tracesSampleRate: 0.1,
      beforeSend: scrubSentryEvent,
    });
  }
}

export const onRequestError = Sentry.captureRequestError;
