"use client";

import { useQuery } from "@tanstack/react-query";
import { adminSentryService, sentryErrorMessage } from "@/services/adminSentry.service";
import { entero, fechaCorta } from "./format";

/**
 * Errores sin resolver de Sentry. Va aparte del monitor, con su propia consulta:
 * si Sentry falla, el resto de la portada sigue funcionando. «No configurado»
 * y «sin errores» son estados distintos y nunca se muestran como lo mismo.
 */
export function SentryErrorsBlock() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["admin-sentry-issues"],
    queryFn: () => adminSentryService.issues(),
    staleTime: 60 * 1000,
    retry: false,
  });

  let cuerpo: React.ReactNode;
  if (isLoading) {
    cuerpo = <p className="text-sm text-vk-text-muted">Cargando…</p>;
  } else if (isError || !data) {
    cuerpo = <p className="text-sm text-vk-warning">{sentryErrorMessage(error)}</p>;
  } else if (!data.configured) {
    cuerpo = (
      <p className="text-sm text-vk-text-muted">
        Sentry no está configurado: faltan SENTRY_AUTH_TOKEN y SENTRY_ORG en el servicio de la
        API. Esto no significa que no haya errores.
      </p>
    );
  } else if (data.issues.length === 0) {
    cuerpo = (
      <p className="text-sm text-vk-text-muted">
        Sin errores sin resolver en los últimos {data.dias} días.
      </p>
    );
  } else {
    cuerpo = (
      <>
        <ul className="space-y-2">
          {data.issues.map((i) => (
            <li key={i.id}>
              <a
                href={i.url}
                target="_blank"
                rel="noopener noreferrer"
                className="block rounded-lg border border-vk-border bg-vk-surface-w px-4 py-3 transition-colors hover:border-vk-blue/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-vk-blue/60"
              >
                <span className="block break-words font-medium text-vk-text-primary">
                  {i.title}
                </span>
                <span className="mt-1 block text-xs text-vk-text-muted">
                  {i.short_id} · {i.project}
                  {i.service ? ` · ${i.service}` : ""} · {entero(i.events)} eventos · último:{" "}
                  {fechaCorta(i.last_seen)}
                </span>
              </a>
            </li>
          ))}
        </ul>
        {data.total > data.issues.length && (
          <p className="text-xs text-vk-text-muted">
            Se muestran {data.issues.length} de {data.total}.
          </p>
        )}
      </>
    );
  }

  return (
    <section className="space-y-3">
      <h2 className="text-sm font-semibold text-vk-text-primary">
        Errores recientes (Sentry)
        {data?.configured && data.total > 0 ? ` (${data.total})` : ""}
      </h2>
      {cuerpo}
    </section>
  );
}
