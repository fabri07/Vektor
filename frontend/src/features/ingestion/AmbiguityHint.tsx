"use client";

import type {
  ColumnMappingSuggestion,
  FieldCatalogEntry,
} from "@/services/ingestion.service";

/**
 * F-M: por qué una columna no se mapeó sola, y entre qué elegir.
 *
 * Vive en UN solo lugar porque el panel renderiza columnas en tres caminos
 * distintos (multi-hoja, lista de revisión y tabla única) y este repo ya pagó
 * el precio de que dos de ellos divergieran.
 *
 * `duda` viaja también sin `options`: son los casos donde Véktor entendió el
 * encabezado y esta hoja no tiene campo donde ponerlo. Ahí no hay entre qué
 * elegir, pero explicarlo es la diferencia entre un hueco y un hueco con motivo.
 */
export function AmbiguityHint({
  suggestion,
  fields,
  onPick,
}: {
  suggestion: ColumnMappingSuggestion;
  fields: FieldCatalogEntry[];
  onPick: (target: string) => void;
}) {
  if (!suggestion.duda) return null;
  const options = suggestion.options ?? [];
  const labelFor = (value: string) => fields.find((f) => f.value === value)?.label ?? value;
  return (
    <div className="rounded border border-vk-blue/30 bg-vk-blue/5 px-2 py-1.5">
      <p className="text-[11px] leading-snug text-vk-text-secondary">{suggestion.duda}</p>
      {options.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {options.map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => onPick(option)}
              className="rounded border border-vk-blue/40 bg-vk-bg-light px-2 py-0.5 text-[11px] text-vk-text-primary hover:border-vk-blue hover:bg-vk-blue/10"
            >
              {labelFor(option)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
