import type { SmartColumn } from "@/components/ui/SmartTable";
import type { EnumOption, FieldDefinition } from "@/types/api";

/**
 * Formatea el valor de un custom field según su data_type.
 * Coerción defensiva: devuelve "—" para null/vacío.
 */
export function formatCustomFieldValue(
  value: unknown,
  dataType: FieldDefinition["data_type"],
  enumOptions: EnumOption[] | null,
): string {
  if (value == null || value === "") return "—";
  switch (dataType) {
    case "number": {
      const n = Number(value);
      return Number.isFinite(n) ? n.toLocaleString("es-AR") : "—";
    }
    case "date": {
      const d = new Date(String(value));
      return isNaN(d.getTime())
        ? String(value)
        : d.toLocaleDateString("es-AR", {
            day: "2-digit",
            month: "short",
            year: "numeric",
          });
    }
    case "boolean":
      return value === true || value === "true" || value === "1" ? "Sí" : "No";
    case "enum":
      return enumOptions?.find((o) => o.value === String(value))?.label ?? String(value);
    case "text":
    default:
      return String(value);
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
