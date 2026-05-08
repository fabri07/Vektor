"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { useToastStore } from "@/stores/toastStore";
import { Button } from "@/components/ui/Button";

const ENTITY_OPTIONS = [
  { value: "sale", label: "Ventas" },
  { value: "expense", label: "Gastos" },
  { value: "product", label: "Productos" },
];

const DATA_TYPE_OPTIONS = [
  { value: "text", label: "Texto" },
  { value: "number", label: "Número" },
  { value: "date", label: "Fecha" },
  { value: "boolean", label: "Sí / No" },
  { value: "enum", label: "Lista de opciones" },
];

const FIELD_KEY_RE = /^[a-z][a-z0-9_]*$/;

interface Props {
  onClose: () => void;
  onCreated: () => void;
}

export function AddCustomFieldModal({ onClose, onCreated }: Props) {
  const addToast = useToastStore((s) => s.add);
  const [entityType, setEntityType] = useState("sale");
  const [fieldKey, setFieldKey] = useState("");
  const [label, setLabel] = useState("");
  const [dataType, setDataType] = useState("text");
  const [enumInput, setEnumInput] = useState("");
  const [enumOptions, setEnumOptions] = useState<Array<{ value: string; label: string }>>([]);

  const fieldKeyError =
    fieldKey.length > 0 && !FIELD_KEY_RE.test(fieldKey)
      ? "Solo minúsculas, números y guión bajo. Debe empezar con letra."
      : null;

  const enumInputError =
    dataType === "enum" && enumOptions.length === 0
      ? "Agregá al menos una opción."
      : null;

  const canSubmit =
    entityType &&
    label.trim().length > 0 &&
    fieldKey.length >= 2 &&
    !fieldKeyError &&
    !enumInputError;

  const addEnumOption = () => {
    const trimmed = enumInput.trim();
    if (!trimmed) return;
    const value = trimmed.toLowerCase().replace(/\s+/g, "_");
    if (enumOptions.some((o) => o.value === value)) return;
    setEnumOptions((prev) => [...prev, { value, label: trimmed }]);
    setEnumInput("");
  };

  const removeEnumOption = (value: string) => {
    setEnumOptions((prev) => prev.filter((o) => o.value !== value));
  };

  const mutation = useMutation({
    mutationFn: () =>
      fieldDefinitionsService.create({
        entity_type: entityType,
        field_key: fieldKey,
        label: label.trim(),
        data_type: dataType,
        enum_options: dataType === "enum" ? enumOptions : undefined,
      }),
    onSuccess: () => {
      addToast("Campo creado.", "success");
      onCreated();
    },
    onError: () => addToast("No se pudo crear el campo.", "error"),
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
      <div
        className="w-full max-w-md rounded-2xl border border-vk-border-w bg-vk-surface-w p-6 shadow-xl"
        role="dialog"
        aria-modal="true"
      >
        <div className="mb-5 flex items-center justify-between">
          <h2 className="text-base font-semibold text-vk-text-primary">
            Nuevo campo personalizado
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-vk-text-muted hover:text-vk-text-secondary"
            aria-label="Cerrar"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-vk-text-secondary">
              Entidad
            </label>
            <select
              value={entityType}
              onChange={(e) => setEntityType(e.target.value)}
              className="w-full rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
            >
              {ENTITY_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>

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
                <option key={o.value} value={o.value}>{o.label}</option>
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
                  onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addEnumOption())}
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
                        onClick={() => removeEnumOption(o.value)}
                        className="hover:text-vk-danger"
                        aria-label={`Eliminar ${o.label}`}
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}
              {enumInputError && (
                <p className="mt-1 text-xs text-vk-danger">{enumInputError}</p>
              )}
            </div>
          )}

          <div>
            <label className="mb-1 block text-sm font-medium text-vk-text-secondary">
              Nombre del campo{" "}
              <span className="font-normal text-vk-text-placeholder">(clave interna)</span>
            </label>
            <input
              type="text"
              value={fieldKey}
              onChange={(e) => setFieldKey(e.target.value.toLowerCase())}
              placeholder="ej: zona_entrega"
              className="w-full rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
            />
            {fieldKeyError && (
              <p className="mt-1 text-xs text-vk-danger">{fieldKeyError}</p>
            )}
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-vk-text-secondary">
              Etiqueta{" "}
              <span className="font-normal text-vk-text-placeholder">(texto visible)</span>
            </label>
            <input
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="ej: Zona de entrega"
              className="w-full rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
            />
          </div>
        </div>

        <div className="mt-6 flex justify-end gap-3">
          <Button variant="secondary" size="sm" onClick={onClose}>
            Cancelar
          </Button>
          <Button
            variant="primary"
            size="sm"
            disabled={!canSubmit}
            loading={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            Crear campo
          </Button>
        </div>
      </div>
    </div>
  );
}
