"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import type { FieldDefinition } from "@/types/api";

const ENTITY_CONFIG: Array<{
  key: string;
  label: string;
  systemFields: string[];
  relations?: Array<{ to: string; label: string }>;
}> = [
  {
    key: "sale",
    label: "Ventas",
    systemFields: ["amount", "quantity", "transaction_date", "payment_method", "notes"],
    relations: [{ to: "product", label: "producto" }],
  },
  {
    key: "expense",
    label: "Gastos",
    systemFields: ["amount", "category", "transaction_date", "description", "supplier_name"],
    relations: [],
  },
  {
    key: "product",
    label: "Productos",
    systemFields: ["name", "sku", "sale_price_ars", "unit_cost_ars", "stock_units", "category"],
    relations: [],
  },
];

const DATA_TYPE_ICON: Record<string, string> = {
  text: "T",
  number: "#",
  date: "D",
  enum: "≡",
  boolean: "✓",
};

function SystemFieldPill({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 py-1.5 px-2 rounded text-xs text-vk-text-secondary">
      <span className="h-4 w-4 rounded bg-vk-border-w flex items-center justify-center text-[9px] font-bold text-vk-text-muted shrink-0">
        F
      </span>
      <span>{label}</span>
    </div>
  );
}

function CustomFieldPill({ field }: { field: FieldDefinition }) {
  return (
    <div className="flex items-center gap-2 py-1.5 px-2 rounded text-xs text-vk-text-primary bg-vk-info-bg/40">
      <span className="h-4 w-4 rounded bg-vk-blue/10 flex items-center justify-center text-[9px] font-bold text-vk-blue shrink-0">
        {DATA_TYPE_ICON[field.data_type] ?? "?"}
      </span>
      <span className="truncate">{field.label}</span>
      {!field.is_base_field && (
        <span className="ml-auto shrink-0 text-[9px] text-vk-text-placeholder">custom</span>
      )}
    </div>
  );
}

function TableCard({
  config,
  customFields,
  showSystem,
}: {
  config: (typeof ENTITY_CONFIG)[number];
  customFields: FieldDefinition[];
  showSystem: boolean;
}) {
  return (
    <div className="rounded-xl border border-vk-border-w bg-vk-surface-w shadow-sm min-w-[200px] flex-1">
      <div className="flex items-center gap-2 rounded-t-xl border-b border-vk-border-w bg-vk-bg-light px-4 py-3">
        <span className="h-2 w-2 rounded-full bg-vk-blue" />
        <span className="text-sm font-semibold text-vk-text-primary">{config.label}</span>
        <span className="ml-auto text-xs text-vk-text-muted">
          {customFields.length} campo{customFields.length !== 1 ? "s" : ""}
        </span>
      </div>
      <div className="p-2 space-y-0.5">
        {showSystem &&
          config.systemFields.map((f) => <SystemFieldPill key={f} label={f} />)}
        {customFields.map((f) => (
          <CustomFieldPill key={`${f.entity_type}-${f.field_key}`} field={f} />
        ))}
        {customFields.length === 0 && !showSystem && (
          <p className="px-2 py-3 text-xs text-vk-text-placeholder text-center">
            Sin campos activos
          </p>
        )}
      </div>
    </div>
  );
}

function RelationLine() {
  return (
    <div className="hidden md:flex flex-col items-center justify-center px-1 gap-1 self-center">
      <div className="w-8 h-px bg-vk-border-w" />
      <svg className="h-3 w-3 text-vk-text-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
      </svg>
    </div>
  );
}

export function SchemaERDView() {
  const [showSystem, setShowSystem] = useState(false);

  const { data: fields = [], isLoading } = useQuery({
    queryKey: ["field-definitions"],
    queryFn: () => fieldDefinitionsService.getAll(),
  });

  const byEntity = fields.reduce<Record<string, FieldDefinition[]>>((acc, f) => {
    acc[f.entity_type] = [...(acc[f.entity_type] ?? []), f];
    return acc;
  }, {});

  return (
    <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6">
      <div className="mb-5 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-vk-text-primary">
            Diagrama de estructura
          </h2>
          <p className="mt-1 text-sm text-vk-text-muted">
            Vista de solo lectura. Campos base en azul claro, campos propios en azul.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowSystem((v) => !v)}
          className="shrink-0 text-xs font-medium text-vk-text-muted hover:text-vk-text-secondary transition-colors border border-vk-border-w rounded-lg px-3 py-1.5"
        >
          {showSystem ? "Ocultar campos sistema" : "Ver campos sistema"}
        </button>
      </div>

      {isLoading ? (
        <p className="text-sm text-vk-text-muted">Cargando diagrama...</p>
      ) : (
        <div className="flex flex-col md:flex-row gap-0 items-stretch overflow-x-auto pb-2">
          {ENTITY_CONFIG.map((config, idx) => (
            <div key={config.key} className="flex flex-col md:flex-row items-stretch">
              <TableCard
                config={config}
                customFields={byEntity[config.key] ?? []}
                showSystem={showSystem}
              />
              {idx < ENTITY_CONFIG.length - 1 && <RelationLine />}
            </div>
          ))}
        </div>
      )}

      <div className="mt-5 flex items-center gap-5 border-t border-vk-border-w pt-4">
        <div className="flex items-center gap-2 text-xs text-vk-text-muted">
          <span className="h-3 w-3 rounded bg-vk-border-w" />
          Campos del sistema (ocultos por defecto)
        </div>
        <div className="flex items-center gap-2 text-xs text-vk-text-muted">
          <span className="h-3 w-3 rounded bg-vk-info-bg border border-vk-blue/20" />
          Campos base del rubro
        </div>
        <div className="flex items-center gap-2 text-xs text-vk-text-muted">
          <span className="h-3 w-3 rounded bg-vk-blue/20" />
          Campos personalizados
        </div>
      </div>
    </div>
  );
}
