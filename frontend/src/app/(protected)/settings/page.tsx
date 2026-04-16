"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Tabs } from "@/components/ui/Tabs";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { workspaceService } from "@/services/workspace.service";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";

const TABS = [
  { id: "general", label: "General" },
  { id: "workspace", label: "Google Workspace" },
];

interface ServiceCardProps {
  icon: React.ReactNode;
  name: string;
  description: string;
  available: boolean;
  connected: boolean;
  connectedEmail?: string | null;
  connectedAt?: string | null;
  needsReconnect?: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
  connectPending: boolean;
  disconnectPending: boolean;
}

function ServiceCard({
  icon,
  name,
  description,
  available,
  connected,
  connectedEmail,
  connectedAt,
  needsReconnect,
  onConnect,
  onDisconnect,
  connectPending,
  disconnectPending,
}: ServiceCardProps) {
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);

  return (
    <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-5">
      <div className="flex items-start gap-4">
        {/* Icon */}
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-vk-bg-light text-lg">
          {icon}
        </div>

        {/* Content */}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-vk-text-primary">
              {name}
            </h3>
            {!available && (
              <Badge variant="default">Proximamente</Badge>
            )}
            {available && needsReconnect && (
              <Badge variant="warning">Reconectar</Badge>
            )}
            {available && connected && !needsReconnect && (
              <Badge variant="success">Conectado</Badge>
            )}
          </div>
          <p className="mt-0.5 text-sm text-vk-text-muted">{description}</p>

          {/* Connected account info */}
          {available && connected && connectedEmail && (
            <div className="mt-3 flex items-center gap-2.5 rounded-lg bg-vk-bg-light px-3 py-2">
              <div className="flex h-6 w-6 items-center justify-center rounded-full bg-vk-blue/10 text-[10px] font-bold text-vk-blue">
                G
              </div>
              <div className="min-w-0">
                <p className="text-xs font-medium text-vk-text-primary truncate">
                  {connectedEmail}
                </p>
                {connectedAt && (
                  <p className="text-[11px] text-vk-text-muted">
                    Conectado el{" "}
                    {new Date(connectedAt).toLocaleDateString("es-AR", {
                      day: "2-digit",
                      month: "long",
                      year: "numeric",
                    })}
                  </p>
                )}
              </div>
            </div>
          )}

          {/* Reconnect warning */}
          {available && needsReconnect && (
            <div className="mt-3 rounded-lg border border-vk-warning/30 bg-vk-warning-bg px-3 py-2.5">
              <p className="text-xs font-medium text-vk-warning">
                El token de Google expiro o fue revocado. Reconecta para restaurar el acceso.
              </p>
            </div>
          )}
        </div>

        {/* Action button */}
        <div className="shrink-0 pt-0.5">
          {!available ? (
            <Button variant="secondary" size="sm" disabled>
              Proximamente
            </Button>
          ) : needsReconnect ? (
            <Button
              variant="primary"
              size="sm"
              loading={connectPending}
              onClick={onConnect}
            >
              Reconectar
            </Button>
          ) : connected ? (
            confirmDisconnect ? (
              <div className="flex items-center gap-2">
                <Button
                  variant="danger"
                  size="sm"
                  loading={disconnectPending}
                  onClick={() => {
                    onDisconnect();
                    setConfirmDisconnect(false);
                  }}
                >
                  Confirmar
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setConfirmDisconnect(false)}
                >
                  Cancelar
                </Button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmDisconnect(true)}
                className="rounded-lg border border-vk-danger/30 px-3 py-1.5 text-xs font-medium text-vk-danger hover:bg-vk-danger-bg disabled:opacity-60 transition-colors"
              >
                Desconectar
              </button>
            )
          ) : (
            <Button
              variant="primary"
              size="sm"
              loading={connectPending}
              onClick={onConnect}
            >
              Conectar
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

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
    onError: () => {
      addToast("No se pudo iniciar la conexion con Google.", "error");
    },
  });

  const disconnectMutation = useMutation({
    mutationFn: () => workspaceService.disconnect(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspace-status"] });
      addToast("Gmail desconectado correctamente.", "success");
    },
    onError: () => {
      addToast("No se pudo desconectar Gmail.", "error");
    },
  });

  if (isLoading) {
    return (
      <div className="py-8 text-center">
        <div className="inline-block h-6 w-6 animate-spin rounded-full border-2 border-vk-border-w border-t-vk-blue" />
      </div>
    );
  }

  const gmailConnected = status?.connected ?? false;
  const needsReconnect = !!status?.last_error_code;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-5">
        <h2 className="text-base font-semibold text-vk-text-primary">
          Servicios de Google
        </h2>
        <p className="mt-0.5 text-sm text-vk-text-muted">
          Conecta los servicios de Google que necesites para potenciar tu negocio.
        </p>
      </div>

      {/* Gmail — available */}
      <ServiceCard
        icon={
          <svg width="20" height="16" viewBox="0 0 20 16" fill="none" aria-hidden="true">
            <path d="M18 0H2C0.9 0 0 0.9 0 2v12c0 1.1 0.9 2 2 2h16c1.1 0 2-0.9 2-2V2c0-1.1-0.9-2-2-2z" fill="#EA4335" fillOpacity="0.1"/>
            <path d="M18 0L10 6L2 0" stroke="#EA4335" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            <path d="M2 0v12" stroke="#EA4335" strokeWidth="1.5" strokeLinecap="round"/>
            <path d="M18 0v12" stroke="#EA4335" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
        }
        name="Gmail"
        description="Lee y clasifica correos de proveedores automaticamente"
        available={true}
        connected={gmailConnected}
        connectedEmail={status?.google_account_email}
        connectedAt={status?.connected_at}
        needsReconnect={needsReconnect}
        onConnect={() => connectMutation.mutate()}
        onDisconnect={() => disconnectMutation.mutate()}
        connectPending={connectMutation.isPending}
        disconnectPending={disconnectMutation.isPending}
      />

      {/* Google Sheets — coming soon */}
      <ServiceCard
        icon={
          <svg width="18" height="20" viewBox="0 0 18 20" fill="none" aria-hidden="true">
            <rect x="1" y="1" width="16" height="18" rx="2" stroke="#34A853" strokeWidth="1.5" fill="#34A853" fillOpacity="0.1"/>
            <line x1="6" y1="6" x2="6" y2="15" stroke="#34A853" strokeWidth="1.2"/>
            <line x1="12" y1="6" x2="12" y2="15" stroke="#34A853" strokeWidth="1.2"/>
            <line x1="3" y1="9" x2="15" y2="9" stroke="#34A853" strokeWidth="1.2"/>
            <line x1="3" y1="12" x2="15" y2="12" stroke="#34A853" strokeWidth="1.2"/>
          </svg>
        }
        name="Google Sheets"
        description="Importa datos desde hojas de calculo"
        available={false}
        connected={false}
        onConnect={() => {}}
        onDisconnect={() => {}}
        connectPending={false}
        disconnectPending={false}
      />

      {/* Google Drive — coming soon */}
      <ServiceCard
        icon={
          <svg width="20" height="18" viewBox="0 0 20 18" fill="none" aria-hidden="true">
            <path d="M6.5 1L1 10.5h6L12.5 1H6.5z" stroke="#4285F4" strokeWidth="1.5" strokeLinejoin="round" fill="#4285F4" fillOpacity="0.1"/>
            <path d="M12.5 1L7 10.5h6L18.5 1H12.5z" stroke="#FBBC05" strokeWidth="1.5" strokeLinejoin="round" fill="#FBBC05" fillOpacity="0.1"/>
            <path d="M1 10.5L4.5 17h11l3.5-6.5H1z" stroke="#34A853" strokeWidth="1.5" strokeLinejoin="round" fill="#34A853" fillOpacity="0.1"/>
          </svg>
        }
        name="Google Drive"
        description="Sincroniza archivos y documentos"
        available={false}
        connected={false}
        onConnect={() => {}}
        onDisconnect={() => {}}
        connectPending={false}
        disconnectPending={false}
      />

      {/* Google Docs — coming soon */}
      <ServiceCard
        icon={
          <svg width="16" height="20" viewBox="0 0 16 20" fill="none" aria-hidden="true">
            <rect x="1" y="1" width="14" height="18" rx="2" stroke="#4285F4" strokeWidth="1.5" fill="#4285F4" fillOpacity="0.1"/>
            <line x1="4" y1="6" x2="12" y2="6" stroke="#4285F4" strokeWidth="1.2" strokeLinecap="round"/>
            <line x1="4" y1="9" x2="12" y2="9" stroke="#4285F4" strokeWidth="1.2" strokeLinecap="round"/>
            <line x1="4" y1="12" x2="9" y2="12" stroke="#4285F4" strokeWidth="1.2" strokeLinecap="round"/>
          </svg>
        }
        name="Google Docs"
        description="Genera reportes directamente en documentos"
        available={false}
        connected={false}
        onConnect={() => {}}
        onDisconnect={() => {}}
        connectPending={false}
        disconnectPending={false}
      />
    </div>
  );
}

function FAQItem({ question, answer }: { question: string; answer: string }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="border-b border-vk-border-w last:border-b-0">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between py-3 text-left text-sm font-medium text-vk-text-primary hover:text-vk-blue transition-colors"
      >
        {question}
        <svg
          className={`h-4 w-4 shrink-0 text-vk-text-muted transition-transform duration-200 ${open ? "rotate-180" : ""}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <p className="pb-3 text-sm text-vk-text-secondary leading-relaxed">
          {answer}
        </p>
      )}
    </div>
  );
}

function GeneralTab() {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const addToast = useToastStore((s) => s.add);
  const [suggestion, setSuggestion] = useState("");
  const [sendingSuggestion, setSendingSuggestion] = useState(false);

  const initials = (user?.full_name ?? "")
    .split(" ")
    .map((w) => w[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);

  const handleSuggestionSubmit = () => {
    if (!suggestion.trim()) return;
    setSendingSuggestion(true);
    // Simulate sending — no backend endpoint yet
    setTimeout(() => {
      addToast("Sugerencia enviada. Gracias por tu feedback.", "success");
      setSuggestion("");
      setSendingSuggestion(false);
    }, 600);
  };

  return (
    <div className="space-y-6">
      {/* ── Foto de perfil + Cuenta ──────────────────────────── */}
      <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6">
        <h2 className="mb-5 text-base font-semibold text-vk-text-primary">
          Cuenta
        </h2>

        {/* Avatar */}
        <div className="mb-6 flex items-center gap-4">
          <div className="relative">
            <div className="flex h-16 w-16 items-center justify-center rounded-full bg-vk-blue/10 text-lg font-bold text-vk-blue">
              {initials || "?"}
            </div>
            <div className="absolute -bottom-0.5 -right-0.5 flex h-6 w-6 items-center justify-center rounded-full border-2 border-vk-surface-w bg-vk-border-w">
              <svg
                className="h-3 w-3 text-vk-text-muted"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
                aria-hidden="true"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"
                />
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"
                />
              </svg>
            </div>
          </div>
          <div>
            <p className="text-sm font-medium text-vk-text-primary">
              {user?.full_name ?? "—"}
            </p>
            <p className="text-xs text-vk-text-muted">
              Foto de perfil (proximamente)
            </p>
          </div>
        </div>

        {/* Account fields */}
        <div className="space-y-3 text-sm">
          <div className="flex items-center justify-between py-2 border-b border-vk-border-w">
            <span className="text-vk-text-muted">Email</span>
            <span className="font-medium text-vk-text-primary">
              {user?.email ?? "—"}
            </span>
          </div>
          <div className="flex items-center justify-between py-2 border-b border-vk-border-w">
            <span className="text-vk-text-muted">Nombre</span>
            <span className="font-medium text-vk-text-primary">
              {user?.full_name ?? "—"}
            </span>
          </div>
          <div className="flex items-center justify-between py-2">
            <span className="text-vk-text-muted">Rol</span>
            <span className="font-medium capitalize text-vk-text-primary">
              {user?.role ?? "—"}
            </span>
          </div>
        </div>
      </div>

      {/* ── Ayuda ────────────────────────────────────────────── */}
      <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6">
        <h2 className="mb-4 text-base font-semibold text-vk-text-primary">
          Ayuda
        </h2>

        <a
          href="#"
          className="mb-4 inline-flex items-center gap-2 text-sm font-medium text-vk-blue hover:underline"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"
            />
          </svg>
          Centro de ayuda
        </a>

        <div className="rounded-lg border border-vk-border-w">
          <div className="px-4">
            <FAQItem
              question="¿Que es el score de salud?"
              answer="El score de salud es un indicador compuesto (0-100) que mide la situacion financiera de tu negocio en base a liquidez, rentabilidad, control de costos, momentum de ventas y cobertura de deuda."
            />
            <FAQItem
              question="¿Como cargo datos?"
              answer="Podes cargar ventas, gastos y productos desde el chat con el asistente o subiendo archivos Excel/CSV desde la seccion de ingestion. Tambien podes registrar movimientos manualmente."
            />
            <FAQItem
              question="¿Como conecto Google Workspace?"
              answer="Anda a la pestana 'Google Workspace' en esta misma pagina de configuracion y hace clic en 'Conectar Google Workspace'. Se te va a redirigir a Google para autorizar el acceso."
            />
            <FAQItem
              question="¿Mis datos estan seguros?"
              answer="Si. Toda la informacion se almacena de forma encriptada y aislada por tenant. Nunca compartimos datos entre negocios ni con terceros."
            />
          </div>
        </div>
      </div>

      {/* ── Terminos y Politicas ─────────────────────────────── */}
      <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6">
        <h2 className="mb-4 text-base font-semibold text-vk-text-primary">
          Terminos y politicas
        </h2>
        <div className="space-y-3">
          <a
            href="#"
            className="flex items-center justify-between rounded-lg px-3 py-2.5 text-sm text-vk-text-primary hover:bg-vk-bg-light transition-colors"
          >
            <span>Terminos de servicio</span>
            <svg
              className="h-4 w-4 text-vk-text-muted"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
            </svg>
          </a>
          <a
            href="#"
            className="flex items-center justify-between rounded-lg px-3 py-2.5 text-sm text-vk-text-primary hover:bg-vk-bg-light transition-colors"
          >
            <span>Politica de privacidad</span>
            <svg
              className="h-4 w-4 text-vk-text-muted"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
            </svg>
          </a>
        </div>
      </div>

      {/* ── Sugerencias y quejas ─────────────────────────────── */}
      <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6">
        <h2 className="mb-1 text-base font-semibold text-vk-text-primary">
          Sugerencias y quejas
        </h2>
        <p className="mb-4 text-sm text-vk-text-muted">
          Contanos como podemos mejorar Vektor.
        </p>
        <textarea
          value={suggestion}
          onChange={(e) => setSuggestion(e.target.value)}
          placeholder="Escribi tu sugerencia o queja..."
          rows={4}
          className="w-full rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-2 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder focus:outline-none focus:ring-2 focus:border-vk-blue/40 focus:ring-vk-blue/15 transition-colors resize-none"
        />
        <div className="mt-3 flex justify-end">
          <Button
            variant="primary"
            size="sm"
            disabled={!suggestion.trim()}
            loading={sendingSuggestion}
            onClick={handleSuggestionSubmit}
          >
            Enviar
          </Button>
        </div>
      </div>

      {/* ── Cerrar sesion ────────────────────────────────────── */}
      <div className="rounded-xl border border-vk-danger/20 bg-vk-surface-w p-6">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold text-vk-text-primary">
              Cerrar sesion
            </h2>
            <p className="mt-0.5 text-sm text-vk-text-muted">
              Se va a cerrar tu sesion en este dispositivo.
            </p>
          </div>
          <Button variant="danger" size="md" onClick={logout}>
            Cerrar sesion
          </Button>
        </div>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState("general");

  return (
    <PageWrapper title="Configuracion">
      <Tabs tabs={TABS} activeTab={activeTab} onChange={setActiveTab} />

      <div className="mt-6">
        {activeTab === "general" && <GeneralTab />}
        {activeTab === "workspace" && <WorkspaceTab />}
      </div>
    </PageWrapper>
  );
}
