"use client";

import { useQuery } from "@tanstack/react-query";
import type { SmartColumn } from "@/components/ui/SmartTable";
import { buildAvailableFieldColumns } from "@/lib/fieldCatalog";
import { buildEditableCustomFieldColumns } from "@/lib/customFieldsEditable";
import {
  fieldCatalogService,
  type AvailableFieldEntityType,
} from "@/services/fieldCatalog.service";
import { useAuthStore } from "@/stores/authStore";

interface Options<T extends object> {
  entityType: AvailableFieldEntityType;
  existingColumns: SmartColumn<T>[];
  onSaveCustomField?: (row: T, values: Record<string, unknown>) => void | Promise<void>;
  isReadOnly?: (row: T) => boolean;
}

/** Una sola fuente para campos del rubro, históricos y editables en las cinco tablas. */
export function useBusinessFieldColumns<T extends object>({
  entityType,
  existingColumns,
  onSaveCustomField,
  isReadOnly,
}: Options<T>): SmartColumn<T>[] {
  const user = useAuthStore((s) => s.user);
  const { data: available = [] } = useQuery({
    queryKey: ["fields-available", entityType, user?.tenant_id, user?.id],
    queryFn: () => fieldCatalogService.getAvailableFields(entityType),
    staleTime: 5 * 60 * 1000,
  });
  const byId = new Map(available.map((f) => [f.field_id, f]));
  const represented = new Set<string>();
  const existing = existingColumns.map((column) => {
    const id = column.fieldId ?? `${entityType}:${column.key}`;
    represented.add(id);
    const field = byId.get(id);
    return field ? {
      ...column,
      fieldId: id,
      header: field.label,
      searchable: field.searchable,
      exportable: field.exportable,
    } : column;
  });
  const extra = available.filter((field) => !represented.has(field.field_id));
  const dynamic = buildAvailableFieldColumns<T>(extra, represented).map((column) => {
    const field = byId.get(column.fieldId! )!;
    if (!onSaveCustomField || !field.editable || !field.enabled_for_new_entries ||
        !field.value_path.startsWith("custom_fields.")) return column;

    // El catálogo de lectura decide si puede editarse. No inferirlo de que
    // exista una definición: los históricos/evidencias pueden ser solo lectura.
    const [editable] = buildEditableCustomFieldColumns<T>([{
      field_key: field.value_path.slice("custom_fields.".length),
      entity_type: entityType,
      label: field.label,
      data_type: field.data_type,
      enum_options: field.enum_options,
      is_required: false,
      display_order: 0,
      is_base_field: false,
      affects_scoring: false,
    }], onSaveCustomField, isReadOnly);
    return editable ? { ...column, render: editable.render } : column;
  });
  return [...existing, ...dynamic];
}
