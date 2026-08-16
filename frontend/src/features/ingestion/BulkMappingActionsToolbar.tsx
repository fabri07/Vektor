"use client";

import type { ColumnMappingSuggestion } from "@/services/ingestion.service";
import {
  acceptAmbiguousSuggestions,
  ignoreEmptyColumns,
  saveUnmappedAsCustomFields,
} from "./bulkMappingActions";

/**
 * F-B: acciones masivas — bajan el tiempo de mapear un archivo con muchas
 * columnas sin resolver, sin pisar nada tocado a mano ni ya sugerido (ver
 * `bulkMappingActions.ts`). Vive en un solo lugar para el camino plano y
 * `SheetMapperSection` (multi-hoja) — mismos botones, mismo criterio.
 *
 * No se renderiza nada si ninguna de las tres tiene algo para hacer: un
 * archivo bien mapeado no necesita ver esta barra.
 */
export function BulkMappingActionsToolbar({
  suggestions,
  mappings,
  onApply,
}: {
  suggestions: ColumnMappingSuggestion[];
  mappings: Record<string, string>;
  onApply: (updates: Record<string, string>) => void;
}) {
  const ambiguas = acceptAmbiguousSuggestions(suggestions, mappings);
  const propias = saveUnmappedAsCustomFields(suggestions, mappings);
  const vacias = ignoreEmptyColumns(suggestions, mappings);

  const nAmbiguas = Object.keys(ambiguas).length;
  const nPropias = Object.keys(propias).length;
  const nVacias = Object.keys(vacias).length;

  if (nAmbiguas === 0 && nPropias === 0 && nVacias === 0) return null;

  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-vk-border-w bg-vk-bg-light/60 px-3 py-2">
      <span className="text-[11px] font-medium text-vk-text-muted">Acciones masivas:</span>
      {nAmbiguas > 0 && (
        <button
          type="button"
          onClick={() => onApply(ambiguas)}
          className="rounded border border-vk-blue/40 bg-vk-surface-w px-2.5 py-1 text-[11px] text-vk-blue transition-colors hover:bg-vk-blue/10"
        >
          Aceptar sugerencias ambiguas ({nAmbiguas})
        </button>
      )}
      {nPropias > 0 && (
        <button
          type="button"
          onClick={() => onApply(propias)}
          className="rounded border border-vk-border-w bg-vk-surface-w px-2.5 py-1 text-[11px] text-vk-text-secondary transition-colors hover:bg-vk-bg-light"
        >
          Guardar sin mapear como campos propios ({nPropias})
        </button>
      )}
      {nVacias > 0 && (
        <button
          type="button"
          onClick={() => onApply(vacias)}
          className="rounded border border-vk-border-w bg-vk-surface-w px-2.5 py-1 text-[11px] text-vk-text-secondary transition-colors hover:bg-vk-bg-light"
        >
          Ignorar columnas vacías ({nVacias})
        </button>
      )}
    </div>
  );
}
