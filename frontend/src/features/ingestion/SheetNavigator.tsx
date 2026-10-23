"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";

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

/** F10-fix contraste: hoja derivada (Ganancias/resumen/balance) — Véktor la
 * calcula sola, excluida por defecto. Distinto motivo que "ignorada" por
 * decisión manual, aunque comparta el mismo status. */
const DERIVED_LABEL = "Derivada — Véktor la calcula sola";

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

  const activeIndex = sheets.findIndex((s) => s.context_id === activeContextId);
  const currentPosition = activeIndex >= 0 ? activeIndex + 1 : 1;

  function goTo(delta: 1 | -1) {
    if (activeIndex < 0) return;
    const next = sheets[activeIndex + delta];
    if (next) onSelect(next.context_id);
  }

  return (
    <div className={["space-y-2", className].join(" ")}>
      {sheets.length > 1 && (
        <div className="flex items-center justify-between text-xs text-vk-text-muted">
          <button
            type="button"
            onClick={() => goTo(-1)}
            disabled={activeIndex <= 0}
            className="flex items-center gap-1 rounded px-1.5 py-0.5 font-medium text-vk-text-secondary hover:bg-vk-bg-light disabled:opacity-40 disabled:hover:bg-transparent"
          >
            <ChevronLeft className="h-3.5 w-3.5" />
            Anterior
          </button>
          <span>
            Hoja {currentPosition} de {sheets.length}
          </span>
          <button
            type="button"
            onClick={() => goTo(1)}
            disabled={activeIndex < 0 || activeIndex >= sheets.length - 1}
            className="flex items-center gap-1 rounded px-1.5 py-0.5 font-medium text-vk-text-secondary hover:bg-vk-bg-light disabled:opacity-40 disabled:hover:bg-transparent"
          >
            Siguiente
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      <div
        role="tablist"
        aria-label="Hojas del archivo"
        className="flex flex-wrap gap-1.5"
      >
        {sheets.map((sheet) => {
          const active = sheet.context_id === activeContextId;
          const title = sheet.is_summary_or_derived ? DERIVED_LABEL : STATUS_LABEL[sheet.status];
          return (
            <button
              key={sheet.context_id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => onSelect(sheet.context_id)}
              title={title}
              // F10-fix contraste: tarjeta clara SÓLIDA (sin fracción de opacidad),
              // mismo patrón que el resto del kit de UI (Modal/Input/Select) —
              // antes `bg-vk-blue/10`/`hover:bg-vk-surface-w/60` dejaban traslucir
              // el canvas oscuro del panel de relectura y el texto quedaba
              // ilegible (oscuro sobre oscuro).
              className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors ${
                active
                  ? "border-vk-blue bg-vk-surface-w text-vk-blue"
                  : "border-vk-border-w bg-vk-surface-w text-vk-text-secondary hover:bg-vk-bg-light"
              }`}
            >
              <span
                className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT_CLASS[sheet.status]}`}
                aria-hidden="true"
              />
              {sheet.label}
              {sheet.is_summary_or_derived && (
                <span className="rounded bg-vk-info-bg px-1 text-[10px] font-normal text-vk-info">
                  Derivada
                </span>
              )}
              <span className="text-vk-text-muted">({sheet.row_count})</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
