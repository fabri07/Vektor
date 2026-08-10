"use client";

import type { ColumnMappingSuggestion } from "@/services/ingestion.service";

const SOURCE_LABELS: Record<string, string> = {
  tenant_history: "Historial",
  heuristic: "Heurística",
  fuzzy: "Similar",
  llm: "IA",
  none: "—",
};

// FASE 2 (A2)/A3: color de la confianza del mapeo (verde alto / ámbar medio / rojo bajo).
function confidenceColor(confidence: number): string {
  if (confidence >= 0.75) return "text-vk-success";
  if (confidence >= 0.5) return "text-vk-warning";
  return "text-vk-danger";
}

/**
 * F-B.0 — de dónde salió el mapeo de una columna y con cuánta confianza.
 *
 * Vive en UN solo lugar porque `ColumnMapperPanel` lo dibujaba dos veces (flujo
 * multi-hoja y tabla única del camino plano) con el mismo JSX copiado, y este
 * repo ya pagó el precio de que dos copias de una fila de mapeo divergieran.
 *
 * No se muestra cuando la columna no quedó mapeada ni cuando no hubo
 * procedencia (`source === "none"`): un porcentaje sin origen no dice nada.
 */
export function MappingOriginHint({
  suggestion,
  mapped,
  className = "",
}: {
  suggestion: ColumnMappingSuggestion;
  /** Status EFECTIVO de la columna (el local, no el que mandó el backend). */
  mapped: boolean;
  /** Clases de posicionamiento del lugar donde se inserta; van adelante. */
  className?: string;
}) {
  if (!mapped || suggestion.source === "none") return null;
  return (
    <p className={[className, "text-[10px] text-vk-text-muted"].filter(Boolean).join(" ")}>
      {SOURCE_LABELS[suggestion.source] ?? suggestion.source} ·{" "}
      <span className={confidenceColor(suggestion.confidence)}>
        {Math.round(suggestion.confidence * 100)}%
      </span>
    </p>
  );
}
