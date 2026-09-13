import type { SmartColumn } from "@/components/ui/SmartTable";
import type { EnumOption, FieldDefinition } from "@/types/api";

/**
 * Formatea el valor de un custom field según su data_type.
 * "—" es SOLO para null/vacío — un valor presente que no encaja en el tipo
 * declarado (ej. texto en una columna "number") se muestra tal cual llegó,
 * nunca como un guión: un guión dice "no hay dato", y acá sí lo hay, solo que
 * no es lo que se esperaba (Cambio 2 del plan de conservación — "mostrar
 * valores no interpretados como originales, no como guiones o ceros").
 */
/**
 * `String(value)` sobre un objeto/array da "[object Object]" — ilegible y, en
 * los hechos, indistinguible de un dato ausente. Un valor original que
 * resultó ser un objeto (un JSON mal aplanado, por ejemplo) se muestra como
 * JSON legible en vez de eso.
 */
function stringifyRaw(value: unknown): string {
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

export function formatCustomFieldValue(
  value: unknown,
  dataType: FieldDefinition["data_type"],
  enumOptions: EnumOption[] | null,
): string {
  if (value == null || value === "") return "—";
  switch (dataType) {
    case "number": {
      const n = Number(value);
      return Number.isFinite(n) ? n.toLocaleString("es-AR") : stringifyRaw(value);
    }
    case "date": {
      const d = new Date(String(value));
      return isNaN(d.getTime())
        ? stringifyRaw(value)
        : d.toLocaleDateString("es-AR", {
            day: "2-digit",
            month: "short",
            year: "numeric",
          });
    }
    case "boolean": {
      if (value === true || value === "true" || value === "1") return "Sí";
      if (value === false || value === "false" || value === "0") return "No";
      return stringifyRaw(value); // no es ni Sí ni No conocido — mostrar el original, no adivinar
    }
    case "enum":
      return enumOptions?.find((o) => o.value === String(value))?.label ?? stringifyRaw(value);
    case "text":
    default:
      return stringifyRaw(value);
  }
}

/**
 * Construye columnas dinámicas de SmartTable a partir de las field-definitions
 * (mismo data source que el ERD). Solo campos custom (no base), ya que los base
 * suelen estar hardcodeados en las columnas de sistema. Ocultas por defecto:
 * el usuario las activa desde el selector de columnas.
 *
 * El valor se lee de `row.custom_fields[field_key]` (la `key` `cf_*` no es una
 * propiedad real de la fila, por eso render/csvValue ignoran `value` y leen la fila).
 */
export function buildCustomFieldColumns<T = Record<string, unknown>>(
  fields: FieldDefinition[],
): SmartColumn<T>[] {
  return [...fields]
    .filter((f) => !f.is_base_field)
    .sort((a, b) => a.display_order - b.display_order)
    .map<SmartColumn<T>>((f) => {
      const read = (row: T): unknown =>
        (row as { custom_fields?: Record<string, unknown> }).custom_fields?.[f.field_key];
      return {
        key: `cf_${f.field_key}`,
        header: f.label,
        hideable: true,
        defaultVisible: false,
        render: (_value: unknown, row: T) =>
          formatCustomFieldValue(read(row), f.data_type, f.enum_options),
        csvValue: (_value: unknown, row: T) =>
          formatCustomFieldValue(read(row), f.data_type, f.enum_options),
      };
    });
}
