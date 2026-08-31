"use client";

import type { ColumnMappingSuggestion } from "@/services/ingestion.service";

import { mappingOrigin, type MappingOrigin } from "./mappingRules";

/**
 * Cómo se le cuenta a la persona de dónde salió el destino de una columna.
 *
 * Son frases, no etiquetas técnicas: «Heurística» no le dice nada a quien sube
 * una planilla, y el nombre de la capa que resolvió el mapeo es un detalle de
 * implementación nuestro.
 *
 * `fuzzy` dice «nombre parecido» y no «los valores» a propósito:
 * `_fuzzy_match` (`column_mapping_service.py`) compara el NOMBRE normalizado
 * del encabezado contra los keywords de cada campo — nunca mira las celdas.
 */
const ORIGIN_LABELS: Record<Exclude<MappingOrigin, null>, string> = {
  user: "Lo elegiste vos",
  // Bloque 5: distinto de `tenant_history` (alias aprendido, difuso) — esto
  // es la MISMA decisión exacta que confirmaste en un archivo con esta misma
  // forma de columnas.
  remembered: "Recordado de una carga anterior con este mismo formato",
  tenant_history: "Usado antes por tu negocio",
  heuristic: "Sugerido por el nombre de la columna",
  fuzzy: "Sugerido por un nombre parecido",
  llm: "Sugerido por Véktor",
  // F-A: nadie reconoció el encabezado. La frase dice lo que va a pasar —el
  // dato se conserva con el nombre que trae el archivo— en vez de afirmar un
  // reconocimiento que no hubo. Es la diferencia entre «lo entendí» y «no lo
  // entendí, pero no lo pierdo».
  auto_custom: "Se guarda con el nombre del archivo",
};

/**
 * F-B.0 — de dónde salió el mapeo de una columna.
 *
 * Vive en UN solo lugar porque `ColumnMapperPanel` lo dibujaba dos veces (flujo
 * multi-hoja y tabla única del camino plano) con el mismo JSX copiado, y este
 * repo ya pagó el precio de que dos copias de una fila de mapeo divergieran.
 *
 * F-B.1 — el porcentaje de confianza salió de la pantalla. No medía nada: la
 * heurística asigna `0.75` fijo a TODO lo que resuelve y el fuzzy escala su
 * ratio a un techo de 65%, así que ningún fuzzy podía superar nunca a ningún
 * heurístico y el 75% no era la probabilidad calibrada de nada. Mostrar un
 * número inventado como si midiera algo es peor que no mostrar nada. Queda en
 * el `title` para depurar, y sigue viajando en el payload (lo consume
 * `column_risk.MappingEntry` en el backend).
 *
 * No se muestra cuando la columna no quedó mapeada ni cuando no hubo
 * procedencia que contar (ver `mappingOrigin`).
 */
export function MappingOriginHint({
  suggestion,
  mapped,
  currentTarget,
  rememberedTarget,
  className = "",
}: {
  suggestion: ColumnMappingSuggestion;
  /** Status EFECTIVO de la columna (el local, no el que mandó el backend). */
  mapped: boolean;
  /** Destino que la columna tiene AHORA, para saber si lo eligió el usuario. */
  currentTarget: string;
  /** Bloque 5: destino recordado de una carga anterior con esta misma columna, si hay. */
  rememberedTarget?: string | null;
  /** Clases de posicionamiento del lugar donde se inserta; van adelante. */
  className?: string;
}) {
  const origin = mappingOrigin(suggestion, currentTarget, rememberedTarget);
  if (!mapped || origin === null) return null;
  return (
    <p
      className={[className, "text-[10px] text-vk-text-muted"].filter(Boolean).join(" ")}
      title={`sugerencia: ${suggestion.source} · ${Math.round(suggestion.confidence * 100)}%`}
    >
      {ORIGIN_LABELS[origin]}
    </p>
  );
}
