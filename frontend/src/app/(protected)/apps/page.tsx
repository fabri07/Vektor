"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { PageWrapper } from "@/components/layout/PageWrapper";
import {
  integrationsService,
  type GoogleConnectionState,
} from "@/services/integrations.service";

function StatusPill({
  tone,
  label,
}: {
  tone: "success" | "warning" | "danger" | "info";
  label: string;
}) {
  const styles = {
    success: "bg-emerald-50 text-emerald-700 ring-emerald-200",
    warning: "bg-amber-50 text-amber-700 ring-amber-200",
    danger: "bg-rose-50 text-rose-700 ring-rose-200",
    info: "bg-blue-50 text-blue-700 ring-blue-200",
  };

  return (
    <span className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${styles[tone]}`}>
      {label}
    </span>
  );
}

function stateToPill(state: GoogleConnectionState, connectionFlowAvailable: boolean) {
  if (state === "CONNECTED") return <StatusPill tone="success" label="Conectado" />;
  if (state === "CONNECTING") return <StatusPill tone="info" label="Conectando..." />;
  if (state === "RECONNECT_REQUIRED" || state === "INSUFFICIENT_SCOPE")
    return <StatusPill tone="warning" label="Reconexión requerida" />;
  if (connectionFlowAvailable) return <StatusPill tone="warning" label="No conectado" />;
  return <StatusPill tone="danger" label="No disponible en este entorno" />;
}

export default function AppsPage() {
  const queryClient = useQueryClient();
  const [connectError, setConnectError] = useState<string | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["google-integrations-status"],
    queryFn: integrationsService.getGoogleStatus,
    staleTime: 10_000,
    // Polling activo mientras el estado es CONNECTING
    refetchInterval: (query) =>
      query.state.data?.connection_state === "CONNECTING" ? 3_000 : false,
  });

  const connectMutation = useMutation({
    mutationFn: integrationsService.startGoogleConnect,
    onSuccess: (result) => {
      setConnectError(null);
      window.open(result.auth_url, "_blank", "noopener,noreferrer");
      void queryClient.invalidateQueries({ queryKey: ["google-integrations-status"] });
    },
    onError: () => {
      setConnectError("No se pudo iniciar la conexión. Intentá de nuevo.");
    },
  });

  const disconnectMutation = useMutation({
    mutationFn: integrationsService.disconnectGoogle,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["google-integrations-status"] });
    },
  });

  const connectionState = data?.connection_state ?? "DISCONNECTED";
  const connectionFlowAvailable = data?.connection_flow_available ?? false;

  const renderActionButton = () => {
    if (!connectionFlowAvailable) return null;

    if (connectionState === "CONNECTED") {
      return (
        <button
          onClick={() => disconnectMutation.mutate()}
          disabled={disconnectMutation.isPending}
          className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm font-medium text-rose-700 hover:bg-rose-100 disabled:opacity-50 transition-colors"
        >
          {disconnectMutation.isPending ? "Desconectando..." : "Desconectar"}
        </button>
      );
    }

    if (connectionState === "CONNECTING") {
      return (
        <button disabled className="rounded-lg border border-blue-200 bg-blue-50 px-4 py-2 text-sm font-medium text-blue-700 opacity-70 cursor-not-allowed">
          Esperando autorización...
        </button>
      );
    }

    const label =
      connectionState === "RECONNECT_REQUIRED" || connectionState === "INSUFFICIENT_SCOPE"
        ? "Reconectar Google"
        : "Conectar Google";

    return (
      <button
        onClick={() => connectMutation.mutate()}
        disabled={connectMutation.isPending}
        className="rounded-lg bg-vk-blue px-4 py-2 text-sm font-medium text-white hover:bg-vk-blue-hover disabled:opacity-50 transition-colors"
      >
        {connectMutation.isPending ? "Iniciando..." : label}
      </button>
    );
  };

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
                Conectá tu cuenta Google para que el asistente acceda a Gmail, Calendar, Sheets y Docs.
              </p>
            </div>
            <div className="flex items-center gap-3">
              {data && stateToPill(connectionState, connectionFlowAvailable)}
              {renderActionButton()}
            </div>
          </div>

          {connectError && (
            <p className="mt-3 text-sm text-rose-600">{connectError}</p>
          )}

          <div className="mt-4 rounded-xl bg-vk-bg-light px-4 py-3 text-sm text-vk-text-secondary">
            {isLoading
              ? "Cargando estado de integraciones..."
              : isError
                ? "No se pudo consultar el estado de Google desde el backend."
                : data?.message}
          </div>

          {data?.connected && data.connected_at && (
            <p className="mt-2 text-xs text-vk-text-muted">
              Conectado el {new Date(data.connected_at).toLocaleDateString("es-AR", {
                day: "numeric",
                month: "long",
                year: "numeric",
              })}
            </p>
          )}
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
                  <StatusPill tone="success" label="Activo" />
                ) : app.needs_reconnect ? (
                  <StatusPill tone="warning" label="Reconectar" />
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
            Si Google no está conectado, el chat te lo marca explícitamente con un botón para conectar
            en lugar de dejar la sección vacía o responder de forma ambigua.
          </p>
        </section>
      </div>
    </PageWrapper>
  );
}
