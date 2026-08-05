/**
 * Reglas de validación del mapeo de columnas, compartidas con el backend.
 *
 * Viven en su propio módulo porque son la lógica que evita que la UI muestre un
 * estado distinto del que envía. Cada una espeja una validación concreta del
 * confirm (`backend/app/api/v1/ingestion.py`): si divergen, el usuario vuelve a
 * chocar contra un 422 que la pantalla decía que no iba a pasar.
 */

import type { FieldCatalogEntry } from "@/services/ingestion.service";

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
 */
export function missingRequiredFields(
  required: string[],
  mappings: Record<string, string>,
): string[] {
  if (Object.keys(mappings).length === 0) return [];
  const cubiertos = new Set(Object.values(mappings).filter(coversRequired));
  return required.filter((r) => !cubiertos.has(r));
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
 */
export function customFieldCollisions(
  mappings: Record<string, string>,
): ScalarCollision[] {
  const porClave = new Map<string, string[]>();
  for (const [col, target] of Object.entries(mappings)) {
    if (!target?.startsWith(CUSTOM_FIELD_PREFIX)) continue;
    const clave = target.slice(CUSTOM_FIELD_PREFIX.length);
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
