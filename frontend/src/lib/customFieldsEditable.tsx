"use client";

import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { Pencil } from "lucide-react";
import type { SmartColumn } from "@/components/ui/SmartTable";
import type { EnumOption, FieldDefinition } from "@/types/api";
import { formatCustomFieldValue } from "@/lib/customFields";

type CustomFieldsRow = { custom_fields?: Record<string, unknown> | null };

function readCustomFields(row: unknown): Record<string, unknown> {
  return ((row as CustomFieldsRow).custom_fields ?? {}) as Record<string, unknown>;
}

function readValue(row: unknown, key: string): unknown {
  return readCustomFields(row)[key];
}

/** Convierte un valor de input crudo al tipo persistible según el data_type. */
function coerce(raw: string, dataType: FieldDefinition["data_type"]): unknown {
  if (raw === "") return null;
  switch (dataType) {
    case "number": {
      const n = Number(raw);
      return Number.isFinite(n) ? n : null;
    }
    case "boolean":
      return raw === "true";
    default:
      return raw;
  }
}

/** Valor inicial del input a partir del valor persistido. */
function toInputValue(value: unknown, dataType: FieldDefinition["data_type"]): string {
  if (value == null) return "";
  if (dataType === "date") {
    const d = new Date(String(value));
    if (!isNaN(d.getTime())) return d.toISOString().slice(0, 10);
  }
  if (dataType === "boolean") {
    return value === true || value === "true" || value === "1" ? "true" : "false";
  }
  return String(value);
}

function CustomFieldEditableCell<T>({
  field,
  row,
  onSave,
}: {
  field: FieldDefinition;
  row: T;
  onSave: (row: T, customFields: Record<string, unknown>) => void | Promise<void>;
}) {
  const value = readValue(row, field.field_key);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement | HTMLSelectElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const start = () => {
    setDraft(toInputValue(value, field.data_type));
    setEditing(true);
  };

  const commit = (raw: string) => {
    setEditing(false);
    // Comparar en formato-input (no coaccionado): evita falsos cambios cuando el
    // valor persistido difiere de tipo/forma del input (date ISO vs YYYY-MM-DD,
    // number-string en JSONB, boolean sin setear). Sin esto, mirar y blurear una
    // celda dispararía un PATCH no-op (y, para boolean vacío, escribiría 'false').
    if (raw === toInputValue(value, field.data_type)) return; // sin cambios reales
    const merged = { ...readCustomFields(row), [field.field_key]: coerce(raw, field.data_type) };
    void onSave(row, merged);
  };

  if (editing) {
    const common =
      "w-full min-w-[7rem] rounded border border-vk-blue/50 bg-vk-surface-w px-2 py-1 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20";

    if (field.data_type === "enum" || field.data_type === "boolean") {
      const options: EnumOption[] =
        field.data_type === "boolean"
          ? [
              { value: "true", label: "Sí" },
              { value: "false", label: "No" },
            ]
          : field.enum_options ?? [];
      return (
        <select
          ref={inputRef as RefObject<HTMLSelectElement>}
          defaultValue={draft}
          className={common}
          // El select commitea en onChange (selección inmediata); onBlur solo
          // cierra para no disparar un segundo PATCH idéntico al perder el foco.
          onBlur={() => setEditing(false)}
          onChange={(e) => commit(e.target.value)}
          onKeyDown={(e) => e.key === "Escape" && setEditing(false)}
        >
          <option value="">—</option>
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      );
    }

    const inputType =
      field.data_type === "number" ? "number" : field.data_type === "date" ? "date" : "text";
    return (
      <input
        ref={inputRef as RefObject<HTMLInputElement>}
        type={inputType}
        defaultValue={draft}
        className={common}
        onBlur={(e) => commit(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit((e.target as HTMLInputElement).value);
          if (e.key === "Escape") setEditing(false);
        }}
      />
    );
  }

  return (
    <button
      type="button"
      onClick={start}
      title="Editar"
      className="group inline-flex max-w-full items-center gap-1.5 rounded px-1.5 py-0.5 text-left text-sm text-vk-text-secondary transition-colors hover:bg-vk-bg-light hover:text-vk-text-primary"
    >
      <span className="truncate">
        {formatCustomFieldValue(value, field.data_type, field.enum_options)}
      </span>
      <Pencil className="h-3 w-3 shrink-0 text-vk-text-placeholder opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
  );
}

/**
 * Igual que `buildCustomFieldColumns` pero con celdas editables inline. Al
 * confirmar una edición llama `onSave(row, mergedCustomFields)` — el page hace
 * el PATCH de la entidad con el dict de custom_fields completo (merge ya hecho).
 */
export function buildEditableCustomFieldColumns<T>(
  fields: FieldDefinition[],
  onSave: (row: T, customFields: Record<string, unknown>) => void | Promise<void>,
  /** Si devuelve true para una fila, sus celdas custom se muestran read-only
   *  (ej. el cliente centinela "Local", que el backend no deja editar). */
  isReadOnly?: (row: T) => boolean,
): SmartColumn<T>[] {
  return [...fields]
    .filter((f) => !f.is_base_field)
    .sort((a, b) => a.display_order - b.display_order)
    .map<SmartColumn<T>>((f) => ({
      key: `cf_${f.field_key}`,
      header: f.label,
      hideable: true,
      defaultVisible: true,
      render: (_value: unknown, row: T) =>
        isReadOnly?.(row) ? (
          <span className="truncate px-1.5 text-sm text-vk-text-secondary">
            {formatCustomFieldValue(
              readValue(row, f.field_key),
              f.data_type,
              f.enum_options,
            )}
          </span>
        ) : (
          <CustomFieldEditableCell field={f} row={row} onSave={onSave} />
        ),
      csvValue: (_value: unknown, row: T) =>
        formatCustomFieldValue(readValue(row, f.field_key), f.data_type, f.enum_options),
    }));
}
