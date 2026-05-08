"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { useToastStore } from "@/stores/toastStore";
import type { FieldDefinition } from "@/types/api";
import { AddCustomFieldModal } from "./AddCustomFieldModal";

const ENTITY_LABELS: Record<string, string> = {
  sale: "Ventas",
  expense: "Gastos",
  product: "Productos",
  inventory: "Inventario",
};

const DATA_TYPE_LABELS: Record<string, string> = {
  text: "Texto",
  number: "Número",
  date: "Fecha",
  enum: "Opciones",
  boolean: "Sí / No",
};

function FieldRow({
  field,
  onToggle,
  onUndo,
  busy,
}: {
  field: FieldDefinition;
  onToggle: (field: FieldDefinition) => void;
  onUndo: (field: FieldDefinition) => void;
  busy: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-3 border-b border-vk-border-w last:border-b-0">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-vk-text-primary truncate">
            {field.label}
          </span>
          {field.is_base_field && (
            <span className="shrink-0 rounded-full bg-vk-info-bg px-2 py-0.5 text-[10px] font-medium text-vk-blue">
              Base
            </span>
          )}
          {field.is_required && (
            <span className="shrink-0 rounded-full bg-vk-warning-bg px-2 py-0.5 text-[10px] font-medium text-vk-warning">
              Obligatorio
            </span>
          )}
        </div>
        <p className="mt-0.5 text-xs text-vk-text-muted">
          {DATA_TYPE_LABELS[field.data_type] ?? field.data_type}
          {field.enum_options && field.enum_options.length > 0 && (
            <span className="ml-1 text-vk-text-placeholder">
              ({field.enum_options.map((o) => o.label).join(", ")})
            </span>
          )}
        </p>
      </div>

      <div className="flex items-center gap-3 shrink-0">
        <button
          type="button"
          disabled={busy}
          onClick={() => onUndo(field)}
          className="text-xs text-vk-text-muted hover:text-vk-blue disabled:opacity-40 transition-colors"
          title="Deshacer último cambio"
        >
          Deshacer
        </button>

        <button
          type="button"
          aria-pressed={true}
          disabled={busy}
          onClick={() => onToggle(field)}
          className="relative h-6 w-10 rounded-full bg-vk-blue transition-colors disabled:opacity-60"
        >
          <span className="absolute top-0.5 right-0.5 h-5 w-5 rounded-full bg-white shadow-sm" />
        </button>
      </div>
    </div>
  );
}

function EntitySection({
  entityType,
  fields,
  onToggle,
  onUndo,
  busy,
}: {
  entityType: string;
  fields: FieldDefinition[];
  onToggle: (field: FieldDefinition) => void;
  onUndo: (field: FieldDefinition) => void;
  busy: boolean;
}) {
  if (fields.length === 0) return null;
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-vk-text-muted">
        {ENTITY_LABELS[entityType] ?? entityType}
      </h3>
      <div className="rounded-lg border border-vk-border-w bg-vk-bg-light px-4">
        {fields.map((f) => (
          <FieldRow
            key={`${f.entity_type}-${f.field_key}`}
            field={f}
            onToggle={onToggle}
            onUndo={onUndo}
            busy={busy}
          />
        ))}
      </div>
    </section>
  );
}

export function FieldDefinitionsPanel() {
  const queryClient = useQueryClient();
  const addToast = useToastStore((s) => s.add);
  const [showAddModal, setShowAddModal] = useState(false);

  const { data: fields = [], isLoading, isError } = useQuery({
    queryKey: ["field-definitions"],
    queryFn: () => fieldDefinitionsService.getAll(),
  });

  const toggleMutation = useMutation({
    mutationFn: (f: FieldDefinition) =>
      fieldDefinitionsService.toggle(f.field_key, f.entity_type, false),
    onSuccess: () => {
      addToast("Campo deshabilitado.", "success");
      void queryClient.invalidateQueries({ queryKey: ["field-definitions"] });
    },
    onError: () => addToast("No se pudo actualizar el campo.", "error"),
  });

  const undoMutation = useMutation({
    mutationFn: (f: FieldDefinition) =>
      fieldDefinitionsService.undo(f.field_key, f.entity_type),
    onSuccess: () => {
      addToast("Cambio deshecho.", "success");
      void queryClient.invalidateQueries({ queryKey: ["field-definitions"] });
    },
    onError: () => addToast("No hay cambios anteriores para deshacer.", "info"),
  });

  const grouped = fields.reduce<Record<string, FieldDefinition[]>>((acc, f) => {
    acc[f.entity_type] = [...(acc[f.entity_type] ?? []), f];
    return acc;
  }, {});

  const busy = toggleMutation.isPending || undoMutation.isPending;
  const entityOrder = ["sale", "expense", "product", "inventory"];

  return (
    <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6">
      <div className="mb-5 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-vk-text-primary">
            Campos personalizados
          </h2>
          <p className="mt-1 text-sm text-vk-text-muted">
            Los campos base vienen del perfil de tu rubro. Podés agregar los tuyos o deshabilitar los que no uses.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowAddModal(true)}
          className="shrink-0 rounded-lg bg-vk-blue px-3 py-2 text-sm font-medium text-white hover:bg-vk-blue-hover transition-colors"
        >
          + Nuevo campo
        </button>
      </div>

      {isLoading && (
        <p className="text-sm text-vk-text-muted">Cargando campos...</p>
      )}
      {isError && (
        <p className="text-sm text-vk-danger">
          No se pudieron cargar los campos.
        </p>
      )}

      {!isLoading && !isError && (
        <div className="space-y-5">
          {entityOrder.map((et) =>
            grouped[et] ? (
              <EntitySection
                key={et}
                entityType={et}
                fields={grouped[et]}
                onToggle={(f) => toggleMutation.mutate(f)}
                onUndo={(f) => undoMutation.mutate(f)}
                busy={busy}
              />
            ) : null
          )}
        </div>
      )}

      {showAddModal && (
        <AddCustomFieldModal
          onClose={() => setShowAddModal(false)}
          onCreated={() => {
            setShowAddModal(false);
            void queryClient.invalidateQueries({ queryKey: ["field-definitions"] });
          }}
        />
      )}
    </div>
  );
}
