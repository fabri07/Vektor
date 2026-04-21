"use client";

import { useQuery } from "@tanstack/react-query";

import { PageWrapper } from "@/components/layout/PageWrapper";
import { integrationsService } from "@/services/integrations.service";

function StatusPill({
  tone,
  label,
}: {
  tone: "success" | "warning" | "danger";
  label: string;
}) {
  const styles = {
    success: "bg-emerald-50 text-emerald-700 ring-emerald-200",
    warning: "bg-amber-50 text-amber-700 ring-amber-200",
    danger: "bg-rose-50 text-rose-700 ring-rose-200",
  };

  return (
    <span className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${styles[tone]}`}>
      {label}
    </span>
  );
}

export default function AppsPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["google-integrations-status"],
    queryFn: integrationsService.getGoogleStatus,
    staleTime: 60_000,
  });

  return (
    <PageWrapper title="Aplicaciones">
      <div className="space-y-6">
        <section className="rounded-2xl border border-vk-border-w bg-vk-surface-w p-6">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-2">
              <h2 className="text-lg font-semibold text-vk-text-primary">
                Integraciones Google
              </h2>
              <p className="max-w-2xl text-sm text-vk-text-muted">
                Estado real del entorno para Gmail, Google Calendar, Sheets y Docs.
              </p>
            </div>
            {data ? (
              data.mcp_enabled && data.mcp_server_configured ? (
                <StatusPill tone="warning" label="Backend listo, flujo pendiente" />
              ) : (
                <StatusPill tone="danger" label="No disponible en este entorno" />
              )
            ) : null}
          </div>

          <div className="mt-4 rounded-xl bg-vk-bg-light px-4 py-3 text-sm text-vk-text-secondary">
            {isLoading
              ? "Cargando estado de integraciones..."
              : isError
                ? "No se pudo consultar el estado de Google desde el backend."
                : data?.message}
          </div>
        </section>

        <section className="grid gap-4 md:grid-cols-3">
          {(data?.apps ?? []).map((app) => (
            <article
              key={app.id}
              className="rounded-2xl border border-vk-border-w bg-vk-surface-w p-5"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="space-y-1">
                  <h3 className="text-base font-semibold text-vk-text-primary">
                    {app.label}
                  </h3>
                  <p className="text-sm text-vk-text-muted">{app.description}</p>
                </div>
                {app.connected ? (
                  <StatusPill tone="success" label="Conectado" />
                ) : app.available ? (
                  <StatusPill tone="warning" label="Disponible" />
                ) : (
                  <StatusPill tone="danger" label="No disponible" />
                )}
              </div>

              <div className="mt-4 space-y-2">
                <p className="text-xs font-medium uppercase tracking-[0.12em] text-vk-text-muted">
                  Scopes requeridos
                </p>
                <div className="flex flex-wrap gap-2">
                  {app.required_scopes.map((scope) => (
                    <span
                      key={scope}
                      className="rounded-full bg-vk-bg-light px-2.5 py-1 text-xs text-vk-text-secondary"
                    >
                      {scope}
                    </span>
                  ))}
                </div>
              </div>
            </article>
          ))}
        </section>

        <section className="rounded-2xl border border-dashed border-vk-border-w bg-vk-surface-w p-6">
          <h2 className="text-base font-semibold text-vk-text-primary">
            Qué esperar desde el chat
          </h2>
          <p className="mt-2 text-sm text-vk-text-muted">
            Si Google no está disponible, el chat ahora te lo marca explícitamente en lugar
            de dejar la sección vacía o responder de forma ambigua.
          </p>
        </section>
      </div>
    </PageWrapper>
  );
}
