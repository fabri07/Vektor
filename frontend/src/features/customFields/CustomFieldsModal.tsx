"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X, Plus, RotateCcw } from "lucide-react";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { useToastStore } from "@/stores/toastStore";
import { Button } from "@/components/ui/Button";
import type { FieldDefinition } from "@/types/api";

const DATA_TYPE_OPTIONS = [
  { value: "text", label: "Texto" },
  { value: "number", label: "Número" },
  { value: "date", label: "Fecha" },
  { value: "boolean", label: "Sí / No" },
  { value: "enum", label: "Lista de opciones" },
];

const FIELD_KEY_RE = /^[a-z][a-z0-9_]*$/;

interface Props {
  entityType: string;
  entityLabel: string;
  onClose: () => void;
  /** Llamado tras crear/deshabilitar/deshacer para refrescar la tabla. */
  onChanged: () => void;
}

export function CustomFieldsModal({ entityType, entityLabel, onClose, onChanged }: Props) {
  const addToast = useToastStore((s) => s.add);
  const queryClient = useQueryClient();

  const { data: fields = [], isLoading } = useQuery({
    queryKey: ["field-definitions", entityType],
    queryFn: () => fieldDefinitionsService.getAll(entityType),
  });

  const customFields = fields.filter((f) => !f.is_base_field);

  // ── Form state ──────────────────────────────────────────────
  const [fieldKey, setFieldKey] = useState("");
  const [label, setLabel] = useState("");
  const [dataType, setDataType] = useState("text");
  const [enumInput, setEnumInput] = useState("");
  const [enumOptions, setEnumOptions] = useState<Array<{ value: string; label: string }>>([]);

  const fieldKeyError =
    fieldKey.length > 0 && !FIELD_KEY_RE.test(fieldKey)
      ? "Solo minúsculas, números y guión bajo. Debe empezar con letra."
      : null;
  const enumError =
    dataType === "enum" && enumOptions.length === 0 ? "Agregá al menos una opción." : null;
  const canSubmit =
    label.trim().length > 0 && fieldKey.length >= 2 && !fieldKeyError && !enumError;

  const addEnumOption = () => {
    const trimmed = enumInput.trim();
    if (!trimmed) return;
    const value = trimmed.toLowerCase().replace(/\s+/g, "_");
    if (enumOptions.some((o) => o.value === value)) return;
    setEnumOptions((prev) => [...prev, { value, label: trimmed }]);
    setEnumInput("");
  };

  const resetForm = () => {
    setFieldKey("");
    setLabel("");
    setDataType("text");
    setEnumInput("");
    setEnumOptions([]);
  };

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["field-definitions", entityType] });
    void queryClient.invalidateQueries({ queryKey: ["field-definitions"] });
    onChanged();
  };

  const createMutation = useMutation({
    mutationFn: () =>
      fieldDefinitionsService.create({
        entity_type: entityType,
        field_key: fieldKey,
        label: label.trim(),
        data_type: dataType,
        enum_options: dataType === "enum" ? enumOptions : undefined,
      }),
    onSuccess: () => {
      addToast("Columna creada.", "success");
      resetForm();
      invalidate();
    },
    onError: () => addToast("No se pudo crear la columna.", "error"),
  });

  const toggleMutation = useMutation({
    mutationFn: (f: FieldDefinition) =>
      fieldDefinitionsService.toggle(f.field_key, f.entity_type, false),
    onSuccess: () => {
      addToast("Columna deshabilitada.", "success");
      invalidate();
    },
    onError: () => addToast("No se pudo actualizar la columna.", "error"),
  });

  const undoMutation = useMutation({
    mutationFn: (f: FieldDefinition) => fieldDefinitionsService.undo(f.field_key, f.entity_type),
    onSuccess: () => {
      addToast("Cambio deshecho.", "success");
      invalidate();
    },
    onError: () => addToast("No hay cambios anteriores para deshacer.", "info"),
  });

  const busy =
    createMutation.isPending || toggleMutation.isPending || undoMutation.isPending;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
      <div
        className="max-h-[90vh] w-full max-w-md overflow-y-auto rounded-2xl border border-vk-border-w bg-vk-surface-w p-6 shadow-xl"
        role="dialog"
        aria-modal="true"
      >
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-base font-semibold text-vk-text-primary">
            Columnas de {entityLabel}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-vk-text-muted hover:text-vk-text-secondary"
            aria-label="Cerrar"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
        <p className="mb-5 text-sm text-vk-text-muted">
          Agregá columnas propias a esta sección. Después podés completar los datos fila
          por fila en la tabla, por import o desde el chat.
        </p>

        {/* ── Columnas existentes ─────────────────────────────── */}
        {!isLoading && customFields.length > 0 && (
          <div className="mb-6">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-vk-text-muted">
              Tus columnas
            </h3>
            <div className="rounded-lg border border-vk-border-w bg-vk-bg-light px-3">
              {customFields.map((f) => (
                <div
                  key={f.field_key}
                  className="flex items-center justify-between gap-3 border-b border-vk-border-w py-2.5 last:border-b-0"
                >
                  <span className="truncate text-sm text-vk-text-primary">{f.label}</span>
                  <div className="flex shrink-0 items-center gap-3">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => undoMutation.mutate(f)}
                      className="inline-flex items-center gap-1 text-xs text-vk-text-muted hover:text-vk-blue disabled:opacity-40"
                      title="Deshacer último cambio"
                    >
                      <RotateCcw className="h-3 w-3" />
                      Deshacer
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => toggleMutation.mutate(f)}
                      className="text-xs font-medium text-vk-danger hover:underline disabled:opacity-40"
                    >
                      Quitar
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Crear columna ───────────────────────────────────── */}
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-vk-text-muted">
          Nueva columna
        </h3>
        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-vk-text-secondary">
              Tipo de dato
            </label>
            <select
              value={dataType}
              onChange={(e) => {
                setDataType(e.target.value);
                setEnumOptions([]);
                setEnumInput("");
              }}
              className="w-full rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
            >
              {DATA_TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>

          {dataType === "enum" && (
            <div>
              <label className="mb-1 block text-sm font-medium text-vk-text-secondary">
                Opciones
              </label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={enumInput}
                  onChange={(e) => setEnumInput(e.target.value)}
                  onKeyDown={(e) =>
                    e.key === "Enter" && (e.preventDefault(), addEnumOption())
                  }
                  placeholder="ej: Norte, Sur, Centro..."
                  className="flex-1 rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
                />
                <button
                  type="button"
                  onClick={addEnumOption}
                  className="rounded-lg bg-vk-border-w px-3 py-2 text-sm font-medium text-vk-text-secondary hover:bg-vk-border-w-hover transition-colors"
                >
                  +
                </button>
              </div>
              {enumOptions.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {enumOptions.map((o) => (
                    <span
                      key={o.value}
                      className="flex items-center gap-1 rounded-full bg-vk-info-bg px-2.5 py-1 text-xs text-vk-blue"
                    >
                      {o.label}
                      <button
                        type="button"
                        onClick={() =>
                          setEnumOptions((prev) => prev.filter((x) => x.value !== o.value))
                        }
                        className="hover:text-vk-danger"
                        aria-label={`Eliminar ${o.label}`}
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          <div>
            <label className="mb-1 block text-sm font-medium text-vk-text-secondary">
              Nombre de la columna{" "}
              <span className="font-normal text-vk-text-placeholder">(visible)</span>
            </label>
            <input
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="ej: Zona de entrega"
              className="w-full rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
            />
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-vk-text-secondary">
              Clave interna{" "}
              <span className="font-normal text-vk-text-placeholder">(sin espacios)</span>
            </label>
            <input
              type="text"
              value={fieldKey}
              onChange={(e) => setFieldKey(e.target.value.toLowerCase())}
              placeholder="ej: zona_entrega"
              className="w-full rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
            />
            {fieldKeyError && <p className="mt-1 text-xs text-vk-danger">{fieldKeyError}</p>}
          </div>
        </div>

        <div className="mt-6 flex justify-end gap-3">
          <Button variant="secondary" size="sm" onClick={onClose}>
            Cerrar
          </Button>
          <Button
            variant="primary"
            size="sm"
            disabled={!canSubmit}
            loading={createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            <Plus className="mr-1 h-4 w-4" />
            Crear columna
          </Button>
        </div>
      </div>
    </div>
  );
}
