import type { SmartColumn } from "@/components/ui/SmartTable";
import type { AvailableField } from "@/services/fieldCatalog.service";
import { formatCustomFieldValue } from "@/lib/customFields";

/**
 * Cambio 2 del plan de conservación de datos — helpers compartidos para
 * construir columnas/detalle desde el catálogo de lectura (Cambio 1). Reusa
 * `formatCustomFieldValue` (ya distingue 0/false de vacío) en vez de duplicar
 * el formateo.
 */

/**
 * Lee el valor de una fila según `value_path`: nombre de atributo real, o
 * `custom_fields.<key>` si vive ahí. Nunca ejecuta código — `value_path` es
 * una ruta de datos, no una expresión (ver domain/business_field_catalog.py).
 */
export function readByValuePath(row: unknown, valuePath: string): unknown {
  const r = row as Record<string, unknown> | null | undefined;
  if (!r) return undefined;
  if (valuePath.startsWith("custom_fields.")) {
    const key = valuePath.slice("custom_fields.".length);
    const cf = r.custom_fields as Record<string, unknown> | null | undefined;
    return cf?.[key];
  }
  return r[valuePath];
}

/**
 * Columnas GENÉRICAS de solo lectura para los campos del catálogo que la
 * página no representa con un renderer propio — el plan pide "conservar los
 * renderizadores específicos actuales" (nombre, SKU con chip, badges de
 * stock, etc.), así que esto es deliberadamente la COLA de la tabla, no un
 * reemplazo de esas columnas.
 *
 * Edición: fuera de alcance acá a propósito. Un campo canónico editable
 * (ej. `barcode`) sigue editándose por su flujo dedicado (modal de la
 * entidad); esto solo lo hace VISIBLE, que es un permiso distinto de
 * editable (ver la nota del plan). Los campos evidencia/adicionales sin
 * revisar (`editable:false`) tampoco tendrían dónde editarse todavía.
 *
 * Vista inicial pequeña (regla del plan, no un motor de cobertura): un campo
 * canónico usa su propio `default_visible`; uno de evidencia o adicional
 * arranca oculto salvo que la propia PÁGINA decida lo contrario para un caso
 * puntual (ej. purchase_base_cost ya es una columna hardcodeada, no pasa
 * por acá).
 */
export function buildAvailableFieldColumns<T extends object>(
  available: AvailableField[],
  excludeFieldIds: ReadonlySet<string>,
): SmartColumn<T>[] {
  return available
    .filter((f) => !excludeFieldIds.has(f.field_id) && f.searchable !== false)
    .map<SmartColumn<T>>((f) => {
      const defaultVisible = f.origin === "canonical" ? f.default_visible : false;
      return {
        key: f.field_id,
        header: f.label,
        hideable: true,
        defaultVisible,
        render: (_value: unknown, row: T) =>
          formatCustomFieldValue(readByValuePath(row, f.value_path), f.data_type, f.enum_options),
        csvValue: (_value: unknown, row: T) =>
          f.exportable
            ? formatCustomFieldValue(readByValuePath(row, f.value_path), f.data_type, f.enum_options)
            : "",
      };
    });
}

/** Un renglón del detalle "Todos los datos" (Cambio 2). */
export interface AllDataRow {
  field_id: string;
  label: string;
  value: unknown;
  formatted: string;
  unit: string | null;
  origin: AvailableField["origin"];
  dataType: AvailableField["data_type"];
  enabledForNewEntries: boolean;
  financialRule: string | null;
}

/**
 * Arma el detalle completo de una fila: TODOS los campos disponibles de la
 * entidad, sin importar si están ocultos en la tabla — "la tabla puede
 * ocultarlo; no puede hacerlo desaparecer" (contrato de cierre del plan).
 */
export function buildAllDataRows(available: AvailableField[], row: unknown): AllDataRow[] {
  return available.map((f) => ({
    field_id: f.field_id,
    label: f.label,
    value: readByValuePath(row, f.value_path),
    formatted: formatCustomFieldValue(
      readByValuePath(row, f.value_path),
      f.data_type,
      f.enum_options,
    ),
    unit: f.unit,
    origin: f.origin,
    dataType: f.data_type,
    enabledForNewEntries: f.enabled_for_new_entries,
    financialRule: f.financial_rule,
  }));
}
