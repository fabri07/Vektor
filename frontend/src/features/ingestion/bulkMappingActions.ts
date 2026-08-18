import type { ColumnMappingSuggestion } from "@/services/ingestion.service";

/**
 * F-B — acciones masivas del panel de mapeo. Puras: reciben `suggestions` +
 * `mappings` actuales y devuelven SOLO los cambios a aplicar — nunca tocan el
 * estado directamente, para poder usarse igual en el camino plano
 * (`mappings` a nivel de archivo) y en `SheetMapperSection` (`mappings` a
 * nivel de hoja).
 *
 * Las tres respetan lo mismo por construcción, no por un chequeo aparte:
 * ninguna toca una columna que YA tiene un target en `mappings` — ni la que
 * el usuario tocó a mano, ni la que ya trae la sugerencia automática
 * aplicada al cargar. Sólo actúan sobre lo que sigue vacío.
 */

const BLANK_VALUES = new Set(["", "none", "nan"]);

function sinMuestraReal(sampleValues: string[]): boolean {
  return (
    sampleValues.length === 0 ||
    sampleValues.every((v) => BLANK_VALUES.has(String(v ?? "").trim().toLowerCase()))
  );
}

/** Mismo slug que usa el commit de campo personalizado manual (`commitCustom`/
 * `UnmappedModal`) — sin espacios, minúsculas. El backend re-canoniza con
 * `custom_field_slug()` antes de persistir; esto es sólo para la UI en vivo. */
export function customFieldSlug(columnName: string): string {
  return `custom_field:${columnName.trim().toLowerCase().replace(/\s+/g, "_")}`;
}

/**
 * Columnas "ambiguo" (Véktor entendió el encabezado, hay 2+ lecturas
 * razonables) sin resolver todavía → toma el candidato de mayor confianza
 * (`options[0]`, el primero que ofrece el `AmbiguityHint`). Nunca decide una
 * columna `unmapped` (sin ningún candidato) ni una ya resuelta.
 */
export function acceptAmbiguousSuggestions(
  suggestions: ColumnMappingSuggestion[],
  mappings: Record<string, string>,
): Record<string, string> {
  const updates: Record<string, string> = {};
  for (const s of suggestions) {
    if (s.status !== "ambiguo") continue;
    if (mappings[s.source_column]) continue;
    const top = s.options?.[0];
    if (top) updates[s.source_column] = top;
  }
  return updates;
}

/**
 * Columnas sin ningún target hoy, CON datos reales → campo propio con el
 * nombre de la columna (mismo criterio que "guardar como campo propio"
 * manual, pero derivado del encabezado en vez de tipeado). Dos exclusiones:
 * "ambiguo" (tiene candidatos concretos entre los que elegir, no es "no
 * reconocida") y sin muestra real (esa es candidata de `ignoreEmptyColumns`,
 * no de ésta — dos recomendaciones para la misma columna vacía no aportan).
 */
export function saveUnmappedAsCustomFields(
  suggestions: ColumnMappingSuggestion[],
  mappings: Record<string, string>,
): Record<string, string> {
  const updates: Record<string, string> = {};
  for (const s of suggestions) {
    if (mappings[s.source_column]) continue;
    if (s.status === "ambiguo") continue;
    if (sinMuestraReal(s.sample_values)) continue;
    updates[s.source_column] = customFieldSlug(s.source_column);
  }
  return updates;
}

/**
 * Columnas sin target Y sin ninguna muestra real de datos → ignorar. Mismo
 * criterio de "vacía" que `_fila_con_contenido` del backend (F-O.4): None,
 * "", espacios o "nan" no cuentan como dato.
 */
export function ignoreEmptyColumns(
  suggestions: ColumnMappingSuggestion[],
  mappings: Record<string, string>,
): Record<string, string> {
  const updates: Record<string, string> = {};
  for (const s of suggestions) {
    if (mappings[s.source_column]) continue;
    if (!sinMuestraReal(s.sample_values)) continue;
    updates[s.source_column] = "ignore";
  }
  return updates;
}
