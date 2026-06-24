"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Button } from "@/components/ui/Button";
import {
  automationsService,
  type AutomationRule,
} from "@/services/automations.service";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";
import { HealthConfigPanel } from "@/features/settings/components/HealthConfigPanel";
import { WorkSchedulePanel } from "@/features/settings/components/WorkSchedulePanel";
import { FiscalConditionPanel } from "@/features/settings/components/FiscalConditionPanel";
import { HelpChat } from "@/features/help/HelpChat";
import { HelpCenter } from "@/features/help/HelpCenter";
import { ManualEntryLauncher } from "@/features/ingestion/ManualEntryLauncher";

const AGENT_LABELS: Record<string, string> = {
  agent_calendar: "Calendar",
  agent_health: "Health",
  agent_supplier: "Supplier",
  agent_stock: "Stock",
  agent_cash: "Cash",
  agent_sync: "Sync",
  agent_unknown: "Otros",
};

function AutomationToggle({
  enabled,
  onChange,
  disabled,
}: {
  enabled: boolean;
  onChange: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      aria-pressed={enabled}
      disabled={disabled}
      onClick={onChange}
      className={`relative h-7 w-12 rounded-full transition-colors disabled:opacity-60 ${
        enabled ? "bg-vk-blue" : "bg-vk-border-w"
      }`}
    >
      <span
        className={`absolute top-1 h-5 w-5 rounded-full bg-white shadow-sm transition-transform ${
          enabled ? "translate-x-5" : "translate-x-1"
        }`}
      />
    </button>
  );
}

function AutomationRuleCard({
  rule,
  onToggle,
  onDelete,
  busy,
}: {
  rule: AutomationRule;
  onToggle: (rule: AutomationRule) => void;
  onDelete: (rule: AutomationRule) => void;
  busy: boolean;
}) {
  const criteria = Object.entries(rule.criteria ?? {})
    .filter(([key]) => !["agent_name", "action_type", "external_system"].includes(key))
    .slice(0, 4);

  return (
    <div className="rounded-lg border border-vk-border-w bg-vk-bg-light p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="text-sm font-semibold text-vk-text-primary">
            {rule.action_type.replaceAll("_", " ")}
          </p>
          <p className="mt-1 text-xs text-vk-text-muted">
            {rule.external_system ?? "Acción interna"} · Riesgo {rule.risk_level}
          </p>
        </div>
        <AutomationToggle
          enabled={rule.enabled}
          disabled={busy}
          onChange={() => onToggle(rule)}
        />
      </div>

      {criteria.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {criteria.map(([key, value]) => (
            <span
              key={key}
              className="rounded-full bg-vk-surface-w px-2.5 py-1 text-xs text-vk-text-secondary"
            >
              {key}: {String(value)}
            </span>
          ))}
        </div>
      )}

      <div className="mt-3 flex items-center justify-between gap-3">
        <p className="text-xs text-vk-text-muted">
          {rule.last_executed_at
            ? `Última ejecución ${new Date(rule.last_executed_at).toLocaleDateString("es-AR")}`
            : "Todavía no se ejecutó automáticamente"}
        </p>
        <button
          type="button"
          onClick={() => onDelete(rule)}
          disabled={busy}
          className="text-xs font-medium text-vk-danger hover:underline disabled:opacity-60"
        >
          Eliminar
        </button>
      </div>
    </div>
  );
}

function AutomationsSection() {
  const queryClient = useQueryClient();
  const addToast = useToastStore((s) => s.add);
  const { data: rules = [], isLoading, isError } = useQuery({
    queryKey: ["automations"],
    queryFn: automationsService.list,
  });

  const toggleMutation = useMutation({
    mutationFn: (rule: AutomationRule) =>
      automationsService.setEnabled(rule.id, !rule.enabled),
    onSuccess: () => {
      addToast("Automatización actualizada.", "success");
      void queryClient.invalidateQueries({ queryKey: ["automations"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (rule: AutomationRule) => automationsService.remove(rule.id),
    onSuccess: () => {
      addToast("Automatización eliminada.", "success");
      void queryClient.invalidateQueries({ queryKey: ["automations"] });
    },
  });

  const grouped = rules.reduce<Record<string, AutomationRule[]>>((acc, rule) => {
    const label = AGENT_LABELS[rule.agent_name] ?? rule.agent_name;
    acc[label] = [...(acc[label] ?? []), rule];
    return acc;
  }, {});

  return (
    <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6">
      <div className="mb-5">
        <h2 className="text-base font-semibold text-vk-text-primary">
          Automatizaciones
        </h2>
        <p className="mt-1 text-sm text-vk-text-muted">
          Activá o pausá acciones que aprobaste para repetirse automáticamente.
        </p>
      </div>

      {isLoading && <p className="text-sm text-vk-text-muted">Cargando automatizaciones...</p>}
      {isError && (
        <p className="text-sm text-vk-danger">
          No se pudieron cargar las automatizaciones. Puede que el feature flag no esté activo.
        </p>
      )}
      {!isLoading && !isError && rules.length === 0 && (
        <p className="text-sm text-vk-text-muted">
          Todavía no hay tareas automatizadas. Cuando confirmes una acción en el chat,
          Véktor te va a preguntar si querés automatizar casos similares.
        </p>
      )}

      <div className="space-y-5">
        {Object.entries(grouped).map(([agent, agentRules]) => (
          <section key={agent}>
            <h3 className="mb-2 text-sm font-semibold text-vk-text-primary">{agent}</h3>
            <div className="space-y-3">
              {agentRules.map((rule) => (
                <AutomationRuleCard
                  key={rule.id}
                  rule={rule}
                  busy={toggleMutation.isPending || deleteMutation.isPending}
                  onToggle={(item) => toggleMutation.mutate(item)}
                  onDelete={(item) => deleteMutation.mutate(item)}
                />
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}

function ManualEntryCTA() {
  return (
    <div className="overflow-hidden rounded-xl border border-vk-blue/30 bg-gradient-to-br from-vk-blue/10 via-vk-surface-w to-vk-surface-w p-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-base font-semibold text-vk-text-primary">
            ¿Preferís cargar a mano?
          </h2>
          <p className="mt-1 text-sm text-vk-text-muted">
            Registrá ventas, gastos y compras de mercadería en segundos, sin esperar a
            automatizar nada.
          </p>
        </div>
        <ManualEntryLauncher variant="primary" />
      </div>
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

function AyudaTab() {
  return (
    <div className="space-y-6">
      {/* Centro de ayuda: FAQs por sección + buscador */}
      <HelpCenter />

      {/* Chat con el agente de ayuda (para dudas no cubiertas en las FAQ) */}
      <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-2">
        <div className="mb-1 px-4 pt-3">
          <h2 className="text-base font-semibold text-vk-text-primary">
            ¿No encontraste tu respuesta?
          </h2>
          <p className="text-sm text-vk-text-muted">
            Preguntale al asistente de ayuda.
          </p>
        </div>
        <div className="h-[480px]">
          <HelpChat />
        </div>
      </div>
    </div>
  );
}

const TABS = [
  { id: "general", label: "General" },
  { id: "automatizaciones", label: "Automatizaciones" },
  { id: "salud", label: "Score de salud" },
  { id: "horarios", label: "Horarios" },
  { id: "ayuda", label: "Ayuda" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<TabId>("general");

  return (
    <PageWrapper title="Configuracion">
      <div className="mb-6 flex gap-1 border-b border-vk-border-w">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
            className={`px-4 py-2.5 text-sm font-medium transition-colors border-b-2 -mb-px ${
              activeTab === tab.id
                ? "border-vk-blue text-vk-blue"
                : "border-transparent text-vk-text-muted hover:text-vk-text-secondary"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "general" && <GeneralTab />}
      {activeTab === "automatizaciones" && (
        <div className="space-y-5">
          <AutomationsSection />
          <ManualEntryCTA />
        </div>
      )}
      {activeTab === "salud" && <HealthConfigPanel />}
      {activeTab === "horarios" && (
        <>
          <WorkSchedulePanel />
          <FiscalConditionPanel />
        </>
      )}
      {activeTab === "ayuda" && <AyudaTab />}
    </PageWrapper>
  );
}
