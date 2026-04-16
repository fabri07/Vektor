"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { workspaceService } from "@/services/workspace.service";
import { useToastStore } from "@/stores/toastStore";
import type { WorkspaceAppStatus } from "@/types/api";

const DEFAULT_APPS: WorkspaceAppStatus[] = [
  {
    id: "gmail",
    label: "Gmail",
    description: "Correos de proveedores y borradores aprobados.",
    available: true,
    connected: false,
    needs_reconnect: false,
    required_scopes: [],
  },
  {
    id: "sheets",
    label: "Google Sheets",
    description: "Hojas para importar ventas, gastos y compras.",
    available: true,
    connected: false,
    needs_reconnect: false,
    required_scopes: [],
  },
  {
    id: "drive",
    label: "Google Drive",
    description: "Archivos compatibles para usar como origen de datos.",
    available: true,
    connected: false,
    needs_reconnect: false,
    required_scopes: [],
  },
  {
    id: "docs",
    label: "Google Docs",
    description: "Reportes ejecutivos en documentos.",
    available: false,
    connected: false,
    needs_reconnect: false,
    required_scopes: [],
  },
];

const APP_ACCENT: Record<string, { letter: string; color: string }> = {
  gmail: { letter: "M", color: "bg-red-50 text-red-600 border-red-100" },
  sheets: { letter: "S", color: "bg-green-50 text-green-700 border-green-100" },
  drive: { letter: "D", color: "bg-blue-50 text-blue-700 border-blue-100" },
  docs: { letter: "D", color: "bg-sky-50 text-sky-700 border-sky-100" },
};

function AppIcon({ appId }: { appId: string }) {
  const accent = APP_ACCENT[appId] ?? { letter: "G", color: "bg-vk-bg-light text-vk-text-secondary border-vk-border-w" };
  return (
    <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border text-sm font-semibold ${accent.color}`}>
      {accent.letter}
    </div>
  );
}

function GoogleAppCard({
  app,
  connectedEmail,
  connectedAt,
  connectPending,
  disconnectPending,
  onConnect,
  onDisconnect,
}: {
  app: WorkspaceAppStatus;
  connectedEmail: string | null | undefined;
  connectedAt: string | null | undefined;
  connectPending: boolean;
  disconnectPending: boolean;
  onConnect: (appId: string) => void;
  onDisconnect: () => void;
}) {
  return (
    <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-4">
          <AppIcon appId={app.id} />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-sm font-semibold text-vk-text-primary">{app.label}</h2>
              {!app.available && <Badge variant="default">Próximamente</Badge>}
              {app.available && app.needs_reconnect && <Badge variant="warning">Reconectar</Badge>}
              {app.available && app.connected && !app.needs_reconnect && <Badge variant="success">Conectado</Badge>}
            </div>
            <p className="mt-1 text-sm text-vk-text-muted">{app.description}</p>

            {app.connected && connectedEmail && (
              <div className="mt-3 rounded-lg bg-vk-bg-light px-3 py-2">
                <p className="truncate text-xs font-medium text-vk-text-primary">{connectedEmail}</p>
                {connectedAt && (
                  <p className="mt-0.5 text-[11px] text-vk-text-muted">
                    Conectado el{" "}
                    {new Date(connectedAt).toLocaleDateString("es-AR", {
                      day: "2-digit",
                      month: "long",
                      year: "numeric",
                    })}
                  </p>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2 sm:pt-0.5">
          {!app.available ? (
            <Button variant="secondary" size="sm" disabled>
              Próximamente
            </Button>
          ) : app.connected && !app.needs_reconnect ? (
            <Button
              variant="ghost"
              size="sm"
              loading={disconnectPending}
              onClick={onDisconnect}
            >
              Desconectar
            </Button>
          ) : (
            <Button
              variant="primary"
              size="sm"
              loading={connectPending}
              onClick={() => onConnect(app.id)}
            >
              {app.needs_reconnect ? "Reconectar" : "Conectar"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

export default function AppsPage() {
  const queryClient = useQueryClient();
  const addToast = useToastStore((s) => s.add);

  const { data: status, isLoading } = useQuery({
    queryKey: ["workspace-status"],
    queryFn: () => workspaceService.getStatus(),
    staleTime: 60 * 1000,
    retry: false,
  });

  const connectMutation = useMutation({
    mutationFn: (appId: string) => workspaceService.getConnectUrl([appId]),
    onSuccess: ({ authorization_url }) => {
      window.location.href = authorization_url;
    },
    onError: () => {
      addToast("No se pudo iniciar la conexión con Google.", "error");
    },
  });

  const disconnectMutation = useMutation({
    mutationFn: () => workspaceService.disconnect(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspace-status"] });
      addToast("Google desconectado correctamente.", "success");
    },
    onError: () => {
      addToast("No se pudo desconectar Google.", "error");
    },
  });

  const apps = status?.apps?.length ? status.apps : DEFAULT_APPS;

  return (
    <PageWrapper title="Aplicaciones">
      <div className="grid gap-4">
        {isLoading ? (
          <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-8 text-center">
            <div className="inline-block h-6 w-6 animate-spin rounded-full border-2 border-vk-border-w border-t-vk-blue" />
          </div>
        ) : (
          apps.map((app) => (
            <GoogleAppCard
              key={app.id}
              app={app}
              connectedEmail={status?.google_account_email}
              connectedAt={status?.connected_at}
              connectPending={connectMutation.isPending}
              disconnectPending={disconnectMutation.isPending}
              onConnect={(appId) => connectMutation.mutate(appId)}
              onDisconnect={() => disconnectMutation.mutate()}
            />
          ))
        )}
      </div>
    </PageWrapper>
  );
}
