import { api } from "@/lib/api";

/**
 * Cambio 1 del plan de conservación de datos — catálogo de LECTURA
 * (`GET /fields/definitions/available`). Distinto de `fieldDefinitions.
 * service.ts` (contrato de EDICIÓN: ERD, panel de campos propios).
 */
export interface AvailableField {
  field_id: string;
  entity_type: string;
  field_key: string;
  label: string;
  data_type: "text" | "number" | "date" | "boolean" | "enum";
  unit: string | null;
  origin: "canonical" | "evidence" | "additional";
  /** Dónde vive el valor: nombre de atributo, o `custom_fields.<key>`. */
  value_path: string;
  enum_options: { value: string; label: string }[] | null;
  editable: boolean;
  exportable: boolean;
  searchable: boolean;
  default_visible: boolean;
  enabled_for_new_entries: boolean;
  financial_rule: string | null;
}

export type AvailableFieldEntityType = "product" | "sale" | "expense" | "customer" | "supplier";

export const fieldCatalogService = {
  async getAvailableFields(entityType: AvailableFieldEntityType): Promise<AvailableField[]> {
    const res = await api.get<AvailableField[]>("/fields/definitions/available", {
      params: { entity_type: entityType },
    });
    return res.data;
  },
};
