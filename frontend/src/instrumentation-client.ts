import * as Sentry from "@sentry/nextjs";
import { scrubSentryEvent } from "@/lib/sentryScrub";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT,
    sendDefaultPii: false,
    sampleRate: 1.0, // errores: 100%, independiente del sampling de performance
    tracesSampleRate: 0.1,
    // Acotado al dominio de la API propia — NO [/.*/] del default, para no
    // mandar sentry-trace/baggage a orígenes de terceros (ej. picsum.photos).
    tracePropagationTargets: [process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"],
    replaysSessionSampleRate: 0.0,
    replaysOnErrorSampleRate: 1.0,
    integrations: [Sentry.replayIntegration({ maskAllText: true, blockAllMedia: true })],
    beforeSend: scrubSentryEvent,
  });
} else if (process.env.NODE_ENV === "development") {
  // eslint-disable-next-line no-console
  console.info("[sentry] deshabilitado: NEXT_PUBLIC_SENTRY_DSN vacío");
}

export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
