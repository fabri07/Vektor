"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Tabs } from "@/components/ui/Tabs";
import { Badge } from "@/components/ui/Badge";
import { workspaceService } from "@/services/workspace.service";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";

const TABS = [
  { id: "general", label: "General" },
  { id: "workspace", label: "Google Workspace" },
];

function WorkspaceTab() {
  const queryClient = useQueryClient();
  const addToast = useToastStore((s) => s.add);

  const { data: status, isLoading } = useQuery({
    queryKey: ["workspace-status"],
    queryFn: () => workspaceService.getStatus(),
    staleTime: 60 * 1000,
    retry: false,
  });

  const connectMutation = useMutation({
    mutationFn: () => workspaceService.getConnectUrl(),
    onSuccess: ({ authorization_url }) => {
      window.location.href = authorization_url;
    },
  });

  const disconnectMutation = useMutation({
    mutationFn: () => workspaceService.disconnect(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspace-status"] });
      addToast("Google Workspace desconectado.", "success");
    },
  });

  if (isLoading) {
    return (
      <div className="py-8 text-center">
        <div className="inline-block h-6 w-6 animate-spin rounded-full border-2 border-vk-border-w border-t-vk-blue" />
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-vk-text-primary">
            Google Workspace
          </h2>
          <p className="mt-0.5 text-sm text-vk-text-muted">
            Conectá tu cuenta de Google para acceder a Gmail y Sheets.
          </p>
        </div>
        {status?.connected ? (
          <Badge variant="success">Conectado</Badge>
        ) : (
          <Badge variant="default">No conectado</Badge>
        )}
      </div>

      {status?.connected ? (
        <div className="space-y-4">
          {/* Account */}
          <div className="flex items-center gap-3 rounded-lg bg-vk-bg-light px-4 py-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-vk-blue/10 text-xs font-bold text-vk-blue">
              G
            </div>
            <div className="min-w-0">
              <p className="text-sm font-medium text-vk-text-primary truncate">
                {status.google_account_email}
              </p>
              {status.connected_at && (
                <p className="text-xs text-vk-text-muted">
                  Conectado el{" "}
                  {new Date(status.connected_at).toLocaleDateString("es-AR", {
                    day: "2-digit",
                    month: "long",
                    year: "numeric",
                  })}
                </p>
              )}
            </div>
          </div>

          {/* Scopes */}
          {status.scopes_granted.length > 0 && (
            <div>
              <p className="mb-2 text-xs font-medium uppercase tracking-widest text-vk-text-muted">
                Permisos activos
              </p>
              <div className="flex flex-wrap gap-1.5">
                {status.scopes_granted.map((scope) => (
                  <Badge key={scope} variant="info">
                    {scope.split("/").pop() ?? scope}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          {/* Reconnect warning */}
          {status.last_error_code && (
            <div className="rounded-lg border border-vk-warning/30 bg-vk-warning-bg px-4 py-3">
              <p className="text-sm font-medium text-vk-warning">
                Reconexión requerida
              </p>
              <p className="mt-0.5 text-xs text-vk-warning/80">
                El token de Google expiró o fue revocado. Reconectá para que el
                asistente pueda acceder a Gmail.
              </p>
              <button
                type="button"
                onClick={() => connectMutation.mutate()}
                disabled={connectMutation.isPending}
                className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-vk-warning px-3 py-1.5 text-xs font-semibold text-white hover:opacity-90 disabled:opacity-60 transition-opacity"
              >
                Reconectar Google
              </button>
            </div>
          )}

          {/* Disconnect */}
          <div className="pt-2">
            <button
              type="button"
              onClick={() => disconnectMutation.mutate()}
              disabled={disconnectMutation.isPending}
              className="rounded-lg border border-vk-danger/30 px-4 py-2 text-sm font-medium text-vk-danger hover:bg-vk-danger-bg disabled:opacity-60 transition-colors"
            >
              {disconnectMutation.isPending ? "Desconectando..." : "Desconectar cuenta"}
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <p className="text-sm text-vk-text-secondary">
            Al conectar Google Workspace, Véktor puede leer tu Gmail para
            detectar correos de proveedores y generar borradores de respuesta.
          </p>
          <button
            type="button"
            onClick={() => connectMutation.mutate()}
            disabled={connectMutation.isPending}
            className="flex items-center gap-2.5 rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-2.5 text-sm font-medium text-vk-text-primary hover:bg-vk-bg-light disabled:opacity-60 transition-colors"
          >
            {connectMutation.isPending ? (
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-vk-border-w border-t-vk-text-muted" />
            ) : (
              <svg width="16" height="16" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                <path d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.875 2.684-6.615Z" fill="#4285F4"/>
                <path d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18Z" fill="#34A853"/>
                <path d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332Z" fill="#FBBC05"/>
                <path d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 6.29C4.672 4.163 6.656 3.58 9 3.58Z" fill="#EA4335"/>
              </svg>
            )}
            {connectMutation.isPending ? "Redirigiendo..." : "Conectar Google Workspace"}
          </button>
        </div>
      )}
    </div>
  );
}

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState("general");
  const user = useAuthStore((s) => s.user);

  return (
    <PageWrapper title="Configuración">
      <Tabs tabs={TABS} activeTab={activeTab} onChange={setActiveTab} />

      <div className="mt-6">
        {activeTab === "general" && (
          <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6">
            <h2 className="mb-4 text-base font-semibold text-vk-text-primary">
              Cuenta
            </h2>
            <div className="space-y-3 text-sm">
              <div className="flex items-center justify-between py-2 border-b border-vk-border-w">
                <span className="text-vk-text-muted">Email</span>
                <span className="font-medium text-vk-text-primary">{user?.email ?? "—"}</span>
              </div>
              <div className="flex items-center justify-between py-2 border-b border-vk-border-w">
                <span className="text-vk-text-muted">Nombre</span>
                <span className="font-medium text-vk-text-primary">{user?.full_name ?? "—"}</span>
              </div>
              <div className="flex items-center justify-between py-2">
                <span className="text-vk-text-muted">Rol</span>
                <span className="font-medium capitalize text-vk-text-primary">{user?.role ?? "—"}</span>
              </div>
            </div>
            <p className="mt-6 text-xs text-vk-text-muted">
              Más opciones de configuración próximamente.
            </p>
          </div>
        )}

        {activeTab === "workspace" && <WorkspaceTab />}
      </div>
    </PageWrapper>
  );
}
