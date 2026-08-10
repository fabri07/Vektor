/**
 * Reglas de validación del mapeo de columnas, compartidas con el backend.
 *
 * Viven en su propio módulo porque son la lógica que evita que la UI muestre un
 * estado distinto del que envía. Cada una espeja una validación concreta del
 * confirm (`backend/app/api/v1/ingestion.py`): si divergen, el usuario vuelve a
 * chocar contra un 422 que la pantalla decía que no iba a pasar.
 */

import type {
  ColumnMappingSuggestion,
  EntityFieldCatalog,
  FieldCatalogEntry,
} from "@/services/ingestion.service";

/** Lo que impide importar una hoja, con el detalle para poder explicarlo. */
export type SheetIssues = {
  missingRequired: string[];
  collisions: ScalarCollision[];
};

export type ScalarCollision = {
  target: string;
  label: string;
  columns: string[];
};

/**
 * ¿Este target cubre un campo requerido?
 *
 * Un `custom_field:` guarda el dato pero NO satisface el requerido — misma regla
 * que `_missing_required` en el backend. Antes la UI contaba cualquier target no
 * vacío como "mapeado", así que mover la columna del nombre del producto a un
 * campo personalizado dejaba `name` descubierto: la pantalla daba el OK y el
 * confirm respondía `Campos requeridos sin mapear: name` sin decir cómo salir
 * (incidente ASTERIA, 2026-07-31).
 */
export const coversRequired = (target: string): boolean =>
  !!target && target !== "ignore" && !target.startsWith("custom_field:");

/**
 * Requeridos de la entidad que ninguna columna cubre con un campo canónico.
 *
 * Sin NINGÚN mapeo no se reporta nada, y eso espeja al backend: el confirm solo
 * valida requeridos cuando llegan `column_mappings` (`if _flat_mappings:` /
 * `if _ctx_mappings:`). Sin mapeo explícito el importador resuelve las columnas
 * por heurística de headers, así que bloquear acá inventaría un problema que el
 * backend no tiene y trabaría el flujo más común: aceptar el mapeo tal como vino.
 *
 * `alternatives` (F-H4) llega del catálogo y es obligatorio a propósito: un
 * default silencioso haría que un call site nuevo bloqueara archivos que el
 * confirm acepta, sin que nada avise. Un requerido se cubre por alternativa solo
 * si están mapeados TODOS sus campos — con el precio unitario pero sin la
 * cantidad no hay total que calcular.
 */
export function missingRequiredFields(
  required: string[],
  mappings: Record<string, string>,
  alternatives: Record<string, string[]>,
): string[] {
  if (Object.keys(mappings).length === 0) return [];
  const cubiertos = new Set(Object.values(mappings).filter(coversRequired));
  return required.filter(
    (r) =>
      !cubiertos.has(r) &&
      !(alternatives[r]?.length && alternatives[r].every((c) => cubiertos.has(c))),
  );
}

/** Un requerido sin cubrir, listo para mostrarse: cómo se llama y qué se pierde. */
export type MissingExplained = {
  /** Nombre técnico. Sirve para enrutar el salto, NO para mostrar. */
  field: string;
  /** Cómo se llama el campo en la pantalla ("Fecha de venta"). */
  label: string;
  /** Qué pasa si no se mapea. Cadena vacía si el backend no escribió motivo. */
  reason: string;
};

/**
 * Traduce los requeridos sin cubrir a algo que se pueda leer y decidir.
 *
 * Función NUEVA y aparte a propósito: `missingRequiredFields` decide QUÉ falta y
 * es lo que bloquea el confirm — su contrato ya está fijado por sus tests y no
 * se toca. Ésta sólo describe lo que aquélla encontró.
 *
 * Existe porque «falta un dato obligatorio, revisá la hoja más arriba» no dice
 * cuál, no dice por qué, y no lleva a ningún lado: la persona queda con el botón
 * apagado y sin una acción concreta. El motivo lo escribe el backend
 * (`required_reason`) porque es consecuencia de una regla del importador.
 *
 * Sin catálogo se devuelve el nombre técnico como label: es peor que el label,
 * pero mucho mejor que no nombrar el campo. El caller no debería llamar acá
 * mientras el catálogo carga (tampoco bloquea sin catálogo).
 */
export function explainMissing(
  missing: string[],
  catalog: EntityFieldCatalog | undefined,
): MissingExplained[] {
  return missing.map((field) => {
    const entry = catalog?.fields.find((f) => f.value === field);
    return {
      field,
      label: entry?.label ?? field,
      reason: entry?.required_reason ?? "",
    };
  });
}

/**
 * Campos de valor único con más de una columna apuntándoles.
 *
 * El importador se quedaba con la primera columna del orden del archivo y
 * descartaba el resto en silencio (`_resolve_target_cols`), así que el valor
 * guardado dependía de cómo estaba ordenada la planilla. Qué campo es escalar lo
 * decide el backend (`single_value` del catálogo), no una lista propia de acá.
 */
export function scalarCollisions(
  fields: FieldCatalogEntry[],
  mappings: Record<string, string>,
): ScalarCollision[] {
  const escalares = new Map(
    fields.filter((f) => f.single_value).map((f) => [f.value, f.label]),
  );
  const porTarget = new Map<string, string[]>();
  for (const [col, target] of Object.entries(mappings)) {
    if (!escalares.has(target)) continue;
    porTarget.set(target, [...(porTarget.get(target) ?? []), col]);
  }
  return [...porTarget.entries()]
    .filter(([, cols]) => cols.length > 1)
    .map(([target, columns]) => ({
      target,
      label: escalares.get(target) ?? target,
      columns,
    }));
}

/**
 * De dónde salió el destino que hoy tiene una columna.
 *
 * `null` = no hay nada que contar (la columna no quedó mapeada, o el backend no
 * reconoció nada y el usuario tampoco eligió).
 */
export type MappingOrigin =
  | "user"
  | "tenant_history"
  | "heuristic"
  | "fuzzy"
  | "llm"
  | null;

/**
 * F-B.1 — quién decidió el destino de esta columna.
 *
 * Se resuelve COMPARANDO el destino actual contra el que sugirió el backend, no
 * con un flag de "el usuario tocó esto". La diferencia importa: si alguien abre
 * el select, prueba otro campo y termina dejando el mismo que había sugerido
 * Véktor, un flag `touched` diría «Lo elegiste vos» sobre un dato que no salió
 * de esa persona. Comparar el valor no puede mentir — o coincide con la
 * sugerencia o no.
 *
 * Una columna sin destino, o mandada a «Ignorar», no tiene procedencia que
 * contar: el importador no va a guardar nada desde ahí.
 */
export function mappingOrigin(
  suggestion: Pick<ColumnMappingSuggestion, "source" | "target_field">,
  currentTarget: string,
): MappingOrigin {
  const actual = currentTarget?.trim() ?? "";
  if (!actual || actual === "ignore") return null;
  // `target_field` viaja como `null` cuando el backend no propuso nada; ahí
  // cualquier destino actual es del usuario.
  if (actual !== (suggestion.target_field ?? "")) return "user";
  return suggestion.source === "none" ? null : suggestion.source;
}

const CUSTOM_FIELD_PREFIX = "custom_field:";

/**
 * Campos PROPIOS con más de una columna apuntándoles.
 *
 * Misma forma que `scalarCollisions` porque es el mismo problema en la otra
 * rama del mapeo: un campo propio guarda un valor por fila, así que de dos
 * columnas al mismo destino sobrevive una sola. Espeja
 * `_colliding_custom_fields` del confirm.
 *
 * Sin esto, escribir dos veces el mismo nombre de campo personalizado
 * (`commitCustom` no chequea contra las otras columnas de la hoja) da el OK en
 * pantalla y 422 al confirmar — exactamente la divergencia que este módulo
 * existe para evitar.
 *
 * El target y la clave se recortan igual que en `parse_target` del backend
 * (`column_mapping_service.py`), donde este mismo bug ya está arreglado. Sin el
 * recorte de la clave, `"custom_field: obs"` y `"custom_field:obs"` eran claves
 * DISTINTAS y las dos columnas pasaban la pantalla para chocar contra el 422 del
 * confirm, que sí las ve como la misma. Y sin el recorte del target,
 * `" custom_field:obs"` ni entraba a esta rama: se iba con los canónicos y
 * `scalarCollisions` lo evaluaba como si fuera un campo del catálogo.
 */
export function customFieldCollisions(
  mappings: Record<string, string>,
): ScalarCollision[] {
  const porClave = new Map<string, string[]>();
  for (const [col, target] of Object.entries(mappings)) {
    const valor = target?.trim() ?? "";
    if (!valor.startsWith(CUSTOM_FIELD_PREFIX)) continue;
    const clave = valor.slice(CUSTOM_FIELD_PREFIX.length).trim();
    porClave.set(clave, [...(porClave.get(clave) ?? []), col]);
  }
  return [...porClave.entries()]
    .filter(([, cols]) => cols.length > 1)
    .map(([clave, columns]) => ({
      target: `${CUSTOM_FIELD_PREFIX}${clave}`,
      label: clave,
      columns,
    }));
}
