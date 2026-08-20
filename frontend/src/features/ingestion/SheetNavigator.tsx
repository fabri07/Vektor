"use client";

import type { RereadSheetStatus } from "@/services/ingestion.service";

/**
 * F-RR Fase 8: pestañas por hoja/contexto de la sesión de relectura, con un
 * punto de estado (misma paleta que `StatusDot`: verde completa, azul
 * requiere revisión, rojo ambigua, gris ignorada) para que el usuario vea de
 * un vistazo cuáles todavía necesitan su atención antes de aplicar.
 */

const STATUS_DOT_CLASS: Record<RereadSheetStatus["status"], string> = {
  completa: "bg-vk-success",
  requiere_revision: "bg-vk-warning",
  ambigua: "bg-vk-danger",
  ignorada: "bg-vk-text-muted",
};

const STATUS_LABEL: Record<RereadSheetStatus["status"], string> = {
  completa: "Completa",
  requiere_revision: "Requiere revisión",
  ambigua: "Ambigua",
  ignorada: "Ignorada (no se importa)",
};

export function SheetNavigator({
  sheets,
  activeContextId,
  onSelect,
  className = "",
}: {
  sheets: RereadSheetStatus[];
  activeContextId: string | null;
  onSelect: (contextId: string) => void;
  className?: string;
}) {
  if (sheets.length === 0) return null;

  return (
    <div
      role="tablist"
      aria-label="Hojas del archivo"
      className={["flex flex-wrap gap-1.5", className].filter(Boolean).join(" ")}
    >
      {sheets.map((sheet) => {
        const active = sheet.context_id === activeContextId;
        return (
          <button
            key={sheet.context_id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onSelect(sheet.context_id)}
            title={STATUS_LABEL[sheet.status]}
            className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors ${
              active
                ? "border-vk-blue bg-vk-blue/10 text-vk-text-primary"
                : "border-vk-border-w text-vk-text-secondary hover:bg-vk-surface-w/60"
            }`}
          >
            <span
              className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT_CLASS[sheet.status]}`}
              aria-hidden="true"
            />
            {sheet.label}
            <span className="text-vk-text-muted">({sheet.row_count})</span>
          </button>
        );
      })}
    </div>
  );
}
