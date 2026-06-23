"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarClock,
  DatabaseZap,
  ExternalLink,
  FileSpreadsheet,
  FileText,
  FolderKanban,
  Mail,
  RefreshCcw,
  ShieldCheck,
} from "lucide-react";

import { PageWrapper } from "@/components/layout/PageWrapper";
import {
  integrationsService,
  type GoogleAppStatus,
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
  if (state === "RECONNECT_REQUIRED" || state === "INSUFFICIENT_SCOPE") {
    return <StatusPill tone="warning" label="Reconexión requerida" />;
  }
  if (connectionFlowAvailable) return <StatusPill tone="warning" label="No conectado" />;
  return <StatusPill tone="danger" label="No disponible" />;
}

function appIcon(appId: string) {
  if (appId === "drive") return FolderKanban;
  if (appId === "gmail") return Mail;
  if (appId === "calendar") return CalendarClock;
  if (appId === "sheets_docs") return FileSpreadsheet;
  return FileText;
}

// URL pública de cada app de Google para abrirla directamente en una pestaña nueva.
const GOOGLE_APP_URLS: Record<string, string> = {
  drive: "https://drive.google.com",
  gmail: "https://mail.google.com",
  calendar: "https://calendar.google.com",
  sheets_docs: "https://docs.google.com",
};

function appExternalUrl(appId: string): string | null {
  return GOOGLE_APP_URLS[appId] ?? null;
}

function OpenAppButton({ appId, label }: { appId: string; label: string }) {
  const url = appExternalUrl(appId);
  if (!url) return null;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1.5 rounded-xl border border-vk-border-w bg-vk-surface-w px-3 py-2 text-sm font-medium text-vk-blue transition-colors hover:bg-vk-blue-subtle"
    >
      <ExternalLink className="h-4 w-4" />
      Abrir {label}
    </a>
  );
}

function appAccent(appId: string) {
  if (appId === "drive") return "from-[#0B57D0] via-[#4285F4] to-[#34A853]";
  if (appId === "gmail") return "from-[#EA4335] to-[#FBBC05]";
  if (appId === "calendar") return "from-[#1A73E8] to-[#8AB4F8]";
  if (appId === "sheets_docs") return "from-[#188038] to-[#34A853]";
  return "from-vk-blue to-vk-blue-light";
}

function appBadge(app: GoogleAppStatus) {
  if (app.connected) return <StatusPill tone="success" label="Activo" />;
  if (app.needs_reconnect) return <StatusPill tone="warning" label="Reconectar" />;
  if (app.available) return <StatusPill tone="info" label="Disponible" />;
  return <StatusPill tone="danger" label="No disponible" />;
}

export default function AppsPage() {
  const queryClient = useQueryClient();
  const [connectError, setConnectError] = useState<string | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["google-integrations-status"],
    queryFn: integrationsService.getGoogleStatus,
    staleTime: 10_000,
    refetchInterval: (query) =>
      query.state.data?.connection_state === "CONNECTING" ? 3_000 : false,
  });

  const connectMutation = useMutation({
    mutationFn: integrationsService.startGoogleConnect,
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
  const apps = useMemo(() => data?.apps ?? [], [data?.apps]);

  const driveCard = useMemo(() => apps.find((app) => app.id === "drive"), [apps]);
  const otherApps = useMemo(() => apps.filter((app) => app.id !== "drive"), [apps]);

  const handleConnect = async () => {
    if (connectMutation.isPending) return;

    setConnectError(null);
    const authWindow = window.open("about:blank", "vektor-google-oauth");

    try {
      const result = await connectMutation.mutateAsync();
      if (authWindow) {
        authWindow.location.href = result.auth_url;
      } else {
        window.location.assign(result.auth_url);
      }
      void queryClient.invalidateQueries({ queryKey: ["google-integrations-status"] });
    } catch {
      authWindow?.close();
    }
  };

  const renderActionButton = () => {
    if (!connectionFlowAvailable) return null;

    if (connectionState === "CONNECTED") {
      return (
        <button
          onClick={() => disconnectMutation.mutate()}
          disabled={disconnectMutation.isPending}
          className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-2.5 text-sm font-medium text-rose-700 transition-colors hover:bg-rose-100 disabled:opacity-50"
        >
          {disconnectMutation.isPending ? "Desconectando..." : "Desconectar Google"}
        </button>
      );
    }

    const label =
      connectionState === "CONNECTING"
        ? "Reintentar autorización"
        : connectionState === "ERROR"
          ? "Reintentar conexión"
          : connectionState === "RECONNECT_REQUIRED" || connectionState === "INSUFFICIENT_SCOPE"
            ? "Reconectar Google"
            : "Conectar Google";

    const buttonClass =
      connectionState === "CONNECTING"
        ? "rounded-xl border border-blue-200 bg-blue-50 px-4 py-2.5 text-sm font-medium text-blue-700 transition-colors hover:bg-blue-100 disabled:opacity-50"
        : "rounded-xl bg-vk-blue px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-vk-blue-hover disabled:opacity-50";

    return (
      <button
        onClick={handleConnect}
        disabled={connectMutation.isPending}
        className={buttonClass}
      >
        {connectMutation.isPending ? "Iniciando..." : label}
      </button>
    );
  };

  return (
    <PageWrapper title="Aplicaciones">
      <div className="space-y-6">
        <section className="relative overflow-hidden rounded-[28px] border border-vk-border-w bg-vk-surface-w p-6 shadow-[0_18px_45px_rgba(15,22,35,0.08)]">
          <div className="absolute right-0 top-0 h-40 w-40 rounded-full bg-[radial-gradient(circle_at_center,_rgba(66,133,244,0.18),_transparent_68%)]" />
          <div className="absolute -bottom-12 left-10 h-28 w-28 rounded-full bg-[radial-gradient(circle_at_center,_rgba(52,168,83,0.14),_transparent_72%)]" />

          <div className="relative flex flex-col gap-6 lg:flex-row lg:items-start lg:justify-between">
            <div className="max-w-2xl space-y-4">
              <div className="inline-flex items-center gap-2 rounded-full border border-vk-border-w bg-vk-bg-light px-3 py-1 text-xs font-medium text-vk-text-secondary">
                <ShieldCheck className="h-4 w-4 text-vk-blue" />
                Conexión centralizada con Google
              </div>

              <div className="flex items-start gap-4">
                <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-2xl bg-[conic-gradient(from_210deg,_#0B57D0,_#4285F4,_#34A853,_#FBBC05,_#EA4335,_#0B57D0)] text-lg font-semibold text-white shadow-sm">
                  G
                </div>
                <div className="space-y-2">
                  <h2 className="text-[1.65rem] font-semibold leading-tight text-vk-text-primary">
                    Conectá Google una sola vez y dejá que Véktor trabaje sobre tus archivos.
                  </h2>
                  <p className="max-w-xl text-sm leading-6 text-vk-text-secondary">
                    Google Drive es la pieza clave: los agentes pueden encontrar archivos del negocio,
                    leerlos y usar su contenido sin que tengas que descargarlos a tu computadora.
                  </p>
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                <span className="rounded-full bg-[#E8F0FE] px-3 py-1 text-xs font-medium text-[#0B57D0]">
                  Drive
                </span>
                <span className="rounded-full bg-[#E6F4EA] px-3 py-1 text-xs font-medium text-[#137333]">
                  Sheets
                </span>
                <span className="rounded-full bg-[#FCE8E6] px-3 py-1 text-xs font-medium text-[#C5221F]">
                  Gmail
                </span>
                <span className="rounded-full bg-[#E8F0FE] px-3 py-1 text-xs font-medium text-[#1A73E8]">
                  Calendar
                </span>
                <span className="rounded-full bg-[#F1F3F4] px-3 py-1 text-xs font-medium text-vk-text-secondary">
                  Docs
                </span>
              </div>
            </div>

            <div className="w-full max-w-[360px] rounded-[24px] border border-vk-border-w bg-[linear-gradient(180deg,#ffffff_0%,#f8fbff_100%)] p-5 shadow-sm">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="text-xs font-medium uppercase tracking-[0.16em] text-vk-text-muted">
                    Estado de conexión
                  </p>
                  <p className="mt-1 text-lg font-semibold text-vk-text-primary">
                    Cuenta Google
                  </p>
                </div>
                {stateToPill(connectionState, connectionFlowAvailable)}
              </div>

              <div className="mt-4 rounded-2xl border border-vk-border-w bg-vk-surface-w p-4">
                <p className="text-sm leading-6 text-vk-text-secondary">
                  {isLoading
                    ? "Cargando estado de integraciones..."
                    : isError
                      ? "No se pudo consultar el estado de Google desde el backend."
                      : data?.message}
                </p>

                {data?.connected && data.connected_at && (
                  <p className="mt-3 text-xs text-vk-text-muted">
                    Conectado el{" "}
                    {new Date(data.connected_at).toLocaleDateString("es-AR", {
                      day: "numeric",
                      month: "long",
                      year: "numeric",
                    })}
                  </p>
                )}
              </div>

              {connectError && (
                <p className="mt-3 text-sm text-rose-600">{connectError}</p>
              )}

              <div className="mt-4 flex flex-wrap gap-3">
                {renderActionButton()}
                {connectionState === "RECONNECT_REQUIRED" || connectionState === "INSUFFICIENT_SCOPE" ? (
                  <div className="inline-flex items-center gap-2 rounded-xl bg-amber-50 px-3 py-2 text-xs font-medium text-amber-800">
                    <RefreshCcw className="h-4 w-4" />
                    Hace falta renovar permisos
                  </div>
                ) : null}
              </div>
            </div>
          </div>
        </section>

        <section className="grid gap-4 lg:grid-cols-[1.15fr_0.85fr]">
          {driveCard ? (
            <article className="overflow-hidden rounded-[26px] border border-vk-border-w bg-vk-surface-w shadow-sm">
              <div className={`h-2 bg-gradient-to-r ${appAccent(driveCard.id)}`} />
              <div className="p-6">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="space-y-2">
                    <div className="flex items-center gap-3">
                      <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-[#E8F0FE] text-[#0B57D0]">
                        <FolderKanban className="h-5 w-5" />
                      </div>
                      <div>
                        <h3 className="text-lg font-semibold text-vk-text-primary">
                          {driveCard.label}
                        </h3>
                        <p className="text-sm text-vk-text-muted">
                          La conexión más importante para traer contexto real del negocio.
                        </p>
                      </div>
                    </div>
                    <p className="max-w-xl text-sm leading-6 text-vk-text-secondary">
                      {driveCard.description}
                    </p>
                  </div>
                  {appBadge(driveCard)}
                </div>

                <div className="mt-5 grid gap-3 sm:grid-cols-3">
                  <div className="rounded-2xl bg-vk-bg-light p-4">
                    <p className="text-xs font-medium uppercase tracking-[0.14em] text-vk-text-muted">
                      Buscar
                    </p>
                    <p className="mt-2 text-sm text-vk-text-secondary">
                      Archivos de ventas, stock, proveedores o reportes históricos.
                    </p>
                  </div>
                  <div className="rounded-2xl bg-vk-bg-light p-4">
                    <p className="text-xs font-medium uppercase tracking-[0.14em] text-vk-text-muted">
                      Leer
                    </p>
                    <p className="mt-2 text-sm text-vk-text-secondary">
                      Contenido de documentos, planillas y archivos del negocio directamente desde Drive.
                    </p>
                  </div>
                  <div className="rounded-2xl bg-vk-bg-light p-4">
                    <p className="text-xs font-medium uppercase tracking-[0.14em] text-vk-text-muted">
                      Extraer
                    </p>
                    <p className="mt-2 text-sm text-vk-text-secondary">
                      Datos útiles para responder, resumir y detectar información financiera relevante.
                    </p>
                  </div>
                </div>

                <div className="mt-5 flex flex-wrap items-center justify-between gap-3">
                  <div className="flex flex-wrap gap-2">
                    {driveCard.required_scopes.map((scope) => (
                      <span
                        key={scope}
                        className="rounded-full bg-[#E8F0FE] px-3 py-1 text-xs font-medium text-[#0B57D0]"
                      >
                        {scope}
                      </span>
                    ))}
                  </div>
                  <OpenAppButton appId={driveCard.id} label={driveCard.label} />
                </div>
              </div>
            </article>
          ) : null}

          <aside className="rounded-[26px] border border-vk-border-w bg-vk-surface-w p-6 shadow-sm">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-vk-blue-subtle text-vk-blue">
                <DatabaseZap className="h-5 w-5" />
              </div>
              <div>
                <h3 className="text-base font-semibold text-vk-text-primary">
                  Qué cambia al conectar
                </h3>
                <p className="text-sm text-vk-text-muted">
                  La conexión se comparte entre las herramientas Google disponibles.
                </p>
              </div>
            </div>

            <div className="mt-5 space-y-3">
              <div className="rounded-2xl border border-vk-border-w bg-vk-bg-light p-4">
                <p className="text-sm font-medium text-vk-text-primary">
                  Menos fricción para el usuario
                </p>
                <p className="mt-1 text-sm text-vk-text-secondary">
                  Ya no hace falta bajar archivos al dispositivo para después volver a subirlos a Véktor.
                </p>
              </div>
              <div className="rounded-2xl border border-vk-border-w bg-vk-bg-light p-4">
                <p className="text-sm font-medium text-vk-text-primary">
                  Más contexto para los agentes
                </p>
                <p className="mt-1 text-sm text-vk-text-secondary">
                  Drive permite recuperar archivos del negocio ya existentes y trabajarlos en contexto.
                </p>
              </div>
              <div className="rounded-2xl border border-vk-border-w bg-vk-bg-light p-4">
                <p className="text-sm font-medium text-vk-text-primary">
                  Estado visible y reconexión simple
                </p>
                <p className="mt-1 text-sm text-vk-text-secondary">
                  Si faltan permisos o la sesión caduca, la app lo muestra y guía la reconexión.
                </p>
              </div>
            </div>
          </aside>
        </section>

        <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {otherApps.map((app) => {
            const Icon = appIcon(app.id);
            return (
              <article
                key={app.id}
                className="overflow-hidden rounded-[24px] border border-vk-border-w bg-vk-surface-w shadow-sm transition-transform duration-200 hover:-translate-y-0.5"
              >
                <div className={`h-1.5 bg-gradient-to-r ${appAccent(app.id)}`} />
                <div className="p-5">
                  <div className="flex items-start justify-between gap-3">
                    <div className="space-y-1">
                      <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-vk-bg-light text-vk-text-primary">
                        <Icon className="h-5 w-5" />
                      </div>
                      <h3 className="text-base font-semibold text-vk-text-primary">
                        {app.label}
                      </h3>
                    </div>
                    {appBadge(app)}
                  </div>

                  <p className="mt-3 text-sm leading-6 text-vk-text-secondary">
                    {app.description}
                  </p>

                  <div className="mt-4 flex flex-wrap gap-2">
                    {app.required_scopes.map((scope) => (
                      <span
                        key={scope}
                        className="rounded-full bg-vk-bg-light px-2.5 py-1 text-xs text-vk-text-secondary"
                      >
                        {scope}
                      </span>
                    ))}
                  </div>

                  <div className="mt-4">
                    <OpenAppButton appId={app.id} label={app.label} />
                  </div>
                </div>
              </article>
            );
          })}
        </section>
      </div>
    </PageWrapper>
  );
}
