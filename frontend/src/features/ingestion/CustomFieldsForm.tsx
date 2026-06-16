"use client";

import { useQuery } from "@tanstack/react-query";

import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import type { FieldDefinition } from "@/types/api";

interface CustomFieldsFormProps {
  entityType: "sale" | "expense" | "product";
  values: Record<string, unknown>;
  onChange: (fieldKey: string, value: unknown) => void;
  errors?: Record<string, string>;
}

/**
 * Renderiza inputs dinámicos para los custom fields por vertical del tenant.
 * Reutilizable por la carga manual (Fase 1) y futuras secciones Clientes/Proveedores.
 * Degradación limpia: si el tenant no tiene definiciones, renderiza null y el
 * formulario base sigue funcionando.
 */
export function CustomFieldsForm({
  entityType,
  values,
  onChange,
  errors,
}: CustomFieldsFormProps) {
  const { data, isLoading } = useQuery({
    queryKey: ["field-definitions", entityType],
    queryFn: () => fieldDefinitionsService.getAll(entityType),
  });

  if (isLoading) return null;

  const definitions = (data ?? [])
    .slice()
    .sort((a, b) => a.display_order - b.display_order);

  if (definitions.length === 0) return null;

  return (
    <div className="flex flex-col gap-3 border-t border-vk-border-w pt-3">
      <p className="text-xs font-medium text-vk-text-secondary">Campos del rubro</p>
      {definitions.map((def) => (
        <CustomFieldInput
          key={def.field_key}
          def={def}
          value={values[def.field_key]}
          error={errors?.[def.field_key]}
          onChange={(v) => onChange(def.field_key, v)}
        />
      ))}
    </div>
  );
}

function CustomFieldInput({
  def,
  value,
  error,
  onChange,
}: {
  def: FieldDefinition;
  value: unknown;
  error?: string;
  onChange: (value: unknown) => void;
}) {
  const label = def.is_required ? `${def.label} *` : def.label;

  switch (def.data_type) {
    case "number":
      return (
        <Input
          type="number"
          step="any"
          label={label}
          error={error}
          value={value === undefined || value === null ? "" : String(value)}
          onChange={(e) =>
            onChange(e.target.value === "" ? undefined : Number(e.target.value))
          }
        />
      );
    case "date":
      return (
        <Input
          type="date"
          label={label}
          error={error}
          value={value === undefined || value === null ? "" : String(value)}
          onChange={(e) => onChange(e.target.value || undefined)}
        />
      );
    case "boolean":
      return (
        <label className="flex items-center gap-2 text-sm text-vk-text-primary">
          <input
            type="checkbox"
            checked={Boolean(value)}
            onChange={(e) => onChange(e.target.checked)}
            className="h-4 w-4 rounded border-vk-border-w"
          />
          {label}
        </label>
      );
    case "enum":
      return (
        <Select
          label={label}
          error={error}
          options={def.enum_options ?? []}
          value={value === undefined || value === null ? "" : String(value)}
          onChange={(v) => onChange(v || undefined)}
        />
      );
    case "text":
    default:
      return (
        <Input
          type="text"
          label={label}
          error={error}
          value={value === undefined || value === null ? "" : String(value)}
          onChange={(e) => onChange(e.target.value || undefined)}
        />
      );
  }
}
