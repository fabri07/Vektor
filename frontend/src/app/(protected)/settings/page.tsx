"use client";

import { useState } from "react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Button } from "@/components/ui/Button";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";

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
              question="¿Como conecto Google?"
              answer="Anda a Aplicaciones en el menu lateral y conecta Gmail, Google Sheets o Google Drive. Se te va a redirigir a Google para autorizar el acceso."
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
  return (
    <PageWrapper title="Configuracion">
      <GeneralTab />
    </PageWrapper>
  );
}
