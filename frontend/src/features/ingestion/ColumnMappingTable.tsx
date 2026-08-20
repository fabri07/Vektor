"use client";

import type { ColumnMappingSuggestion, FieldCatalogEntry } from "@/services/ingestion.service";

import { AmbiguityHint } from "./AmbiguityHint";
import { MappingOriginHint } from "./MappingOriginHint";
import { StatusDot } from "./StatusDot";
import { TargetSelect } from "./TargetSelect";

/**
 * F-RR Fase 8: tabla columna↔campo para la revisión de UNA hoja/contexto.
 *
 * A propósito NO es la extracción del bloque duplicado de `ColumnMapperPanel`
 * (Fase 7 original del plan): ese archivo tiene 2508 líneas y es el camino de
 * carga inicial, ya en producción — tocarlo para una extracción "prop-driven"
 * de dos variantes que difieren en `showCustomFieldOption`/`sample_values`/
 * header sin haber leído el JSX completo es un riesgo de regresión que no
 * corresponde correr acá. Esta tabla es nueva y autocontenida para la
 * relectura; reusa los mismos sub-componentes (`TargetSelect`,
 * `AmbiguityHint`, `MappingOriginHint`, `StatusDot`) que ya son compartidos.
 * La extracción real queda pendiente como refactor de bajo riesgo aparte.
 */
export function ColumnMappingTable({
  suggestions,
  fields,
  mapping,
  onMappingChange,
  loading = false,
  className = "",
}: {
  suggestions: ColumnMappingSuggestion[];
  fields: FieldCatalogEntry[];
  /** Target EFECTIVO actual por columna (borrador del usuario, ya mergeado con la sugerencia). */
  mapping: Record<string, string>;
  onMappingChange: (sourceColumn: string, target: string) => void;
  loading?: boolean;
  className?: string;
}) {
  if (loading) {
    return (
      <div className={["py-4 text-center text-xs text-vk-text-muted", className].join(" ")}>
        Cargando columnas…
      </div>
    );
  }

  if (suggestions.length === 0) {
    return (
      <div className={["py-4 text-center text-xs text-vk-text-muted", className].join(" ")}>
        Esta hoja no tiene columnas para mapear.
      </div>
    );
  }

  return (
    <div className={["overflow-x-auto", className].join(" ")}>
      <table className="w-full min-w-[520px] text-left text-xs">
        <thead>
          <tr className="border-b border-vk-border-w text-vk-text-muted">
            <th className="py-1.5 pr-2 font-medium">Columna del archivo</th>
            <th className="py-1.5 pl-2 font-medium">Campo en Véktor</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-vk-border-w/50">
          {suggestions.map((s) => {
            const currentTarget = mapping[s.source_column] ?? s.target_field ?? "";
            const mapped = currentTarget !== "" && currentTarget !== "ignore";
            return (
              <tr key={s.source_column} data-testid="column-mapping-row">
                <td className="py-2 pr-2 align-top">
                  <div className="flex items-center gap-1.5">
                    <StatusDot status={s.status} />
                    <span className="font-mono text-vk-text-secondary">{s.source_column}</span>
                  </div>
                  {s.sample_values.length > 0 && (
                    <p className="mt-0.5 truncate text-[10px] text-vk-text-muted">
                      {s.sample_values.slice(0, 3).join(" · ")}
                    </p>
                  )}
                </td>
                <td className="py-2 pl-2 align-top">
                  <TargetSelect
                    target={currentTarget}
                    onChange={(value) => onMappingChange(s.source_column, value)}
                    fields={fields}
                    className="w-full rounded border border-vk-border-w bg-vk-bg-light px-1.5 py-1 text-xs text-vk-text-primary"
                    unknownTarget="custom-only"
                  />
                  <AmbiguityHint
                    suggestion={s}
                    fields={fields}
                    onPick={(target) => onMappingChange(s.source_column, target)}
                  />
                  <MappingOriginHint
                    suggestion={s}
                    mapped={mapped}
                    currentTarget={currentTarget}
                    className="mt-0.5 block"
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
