"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle, AlertCircle, XCircle, ArrowRight } from "lucide-react";
import {
  ingestionService,
  type ColumnMappingSuggestion,
  type ColumnMapping,
} from "@/services/ingestion.service";

// Campos canónicos por entity_type (para los selects del panel derecho)
const CANONICAL_FIELDS: Record<string, Array<{ value: string; label: string }>> = {
  sale: [
    { value: "amount", label: "Monto de venta" },
    { value: "transaction_date", label: "Fecha de venta" },
    { value: "quantity", label: "Cantidad" },
    { value: "payment_method", label: "Método de pago" },
    { value: "product_name", label: "Nombre del producto" },
    { value: "notes", label: "Notas" },
  ],
  expense: [
    { value: "amount", label: "Monto del gasto" },
    { value: "expense_date", label: "Fecha del gasto" },
    { value: "category", label: "Categoría" },
    { value: "supplier_name", label: "Proveedor" },
    { value: "notes", label: "Notas" },
  ],
  product: [
    { value: "sku", label: "Código (SKU)" },
    { value: "name", label: "Nombre" },
    { value: "sale_price_ars", label: "Precio de venta" },
    { value: "unit_cost_ars", label: "Costo unitario" },
    { value: "stock_units", label: "Stock (unidades)" },
    { value: "category", label: "Categoría" },
    { value: "description", label: "Descripción" },
  ],
};

const ENTITY_TYPE_LABELS: Record<string, string> = {
  sale: "Ventas",
  expense: "Gastos",
  product: "Productos",
};

const SOURCE_LABELS: Record<string, string> = {
  tenant_history: "Historial",
  heuristic: "Heurística",
  fuzzy: "Similar",
  none: "—",
};

function StatusDot({ status }: { status: ColumnMappingSuggestion["status"] }) {
  if (status === "mapped") {
    return <span className="h-2 w-2 rounded-full bg-vk-success shrink-0" title="Mapeado" />;
  }
  if (status === "required_missing") {
    return (
      <span
        className="h-2 w-2 rounded-full bg-vk-danger shrink-0"
        title="Campo requerido faltante"
      />
    );
  }
  return <span className="h-2 w-2 rounded-full bg-vk-warning shrink-0" title="Sin mapear" />;
}

// Modal secuencial para columnas sin mapear
function UnmappedModal({
  column,
  entityType,
  onResolve,
  onClose,
}: {
  column: string;
  entityType: string;
  onResolve: (target: string) => void;
  onClose: () => void;
}) {
  const [selected, setSelected] = useState("");
  const [customKey, setCustomKey] = useState("");
  const [mode, setMode] = useState<"field" | "custom" | "ignore">("field");

  const fields = CANONICAL_FIELDS[entityType] ?? [];

  function handleConfirm() {
    if (mode === "field" && selected) {
      onResolve(selected);
    } else if (mode === "custom" && customKey.trim()) {
      onResolve(`custom_field:${customKey.trim().toLowerCase().replace(/\s+/g, "_")}`);
    } else if (mode === "ignore") {
      onResolve("ignore");
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-xl border border-vk-border-w bg-vk-surface-w p-6 shadow-xl">
        <div className="mb-4 flex items-start justify-between gap-2">
          <div>
            <p className="text-xs text-vk-text-muted">Columna sin mapear</p>
            <p className="mt-0.5 font-mono text-sm font-semibold text-vk-text-primary">
              {column}
            </p>
          </div>
          <AlertCircle className="h-5 w-5 shrink-0 text-vk-warning" />
        </div>

        <div className="mb-4 space-y-2">
          <button
            type="button"
            onClick={() => setMode("field")}
            className={`w-full rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
              mode === "field"
                ? "border-vk-blue bg-vk-info-bg text-vk-blue"
                : "border-vk-border-w text-vk-text-secondary hover:bg-vk-bg-light"
            }`}
          >
            Asignar a un campo de Véktor
          </button>
          {mode === "field" && (
            <select
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              className="w-full rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary focus:border-vk-blue focus:outline-none"
            >
              <option value="">Elegir campo...</option>
              {fields.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          )}

          <button
            type="button"
            onClick={() => setMode("custom")}
            className={`w-full rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
              mode === "custom"
                ? "border-vk-blue bg-vk-info-bg text-vk-blue"
                : "border-vk-border-w text-vk-text-secondary hover:bg-vk-bg-light"
            }`}
          >
            Guardar como campo personalizado
          </button>
          {mode === "custom" && (
            <input
              type="text"
              value={customKey}
              onChange={(e) => setCustomKey(e.target.value)}
              placeholder="nombre_del_campo (sin espacios)"
              className="w-full rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary placeholder:text-vk-text-muted focus:border-vk-blue focus:outline-none"
            />
          )}

          <button
            type="button"
            onClick={() => setMode("ignore")}
            className={`w-full rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
              mode === "ignore"
                ? "border-vk-border-w bg-vk-bg-light text-vk-text-muted"
                : "border-vk-border-w text-vk-text-secondary hover:bg-vk-bg-light"
            }`}
          >
            Ignorar esta columna
          </button>
        </div>

        <div className="flex gap-2">
          <button
            type="button"
            onClick={onClose}
            className="flex-1 rounded-lg border border-vk-border-w px-3 py-2 text-sm text-vk-text-secondary hover:bg-vk-bg-light transition-colors"
          >
            Cancelar
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={
              (mode === "field" && !selected) ||
              (mode === "custom" && !customKey.trim())
            }
            className="flex-1 rounded-lg bg-vk-blue px-3 py-2 text-sm font-medium text-white hover:bg-vk-blue-hover disabled:opacity-50 transition-colors"
          >
            Confirmar
          </button>
        </div>
      </div>
    </div>
  );
}

interface ColumnMapperPanelProps {
  fileId: string;
  onDone: () => void;
}

export function ColumnMapperPanel({ fileId, onDone }: ColumnMapperPanelProps) {
  const queryClient = useQueryClient();
  const [confirmedFields, setConfirmedFields] = useState({
    ventas: true,
    gastos: false,
    productos: false,
  });
  const [mappings, setMappings] = useState<Record<string, string>>({});
  const [unmappedQueue, setUnmappedQueue] = useState<string[]>([]);
  const [showUnmappedModal, setShowUnmappedModal] = useState(false);
  const [initialized, setInitialized] = useState(false);

  const { data: preview } = useQuery({
    queryKey: ["ingestion-preview", fileId],
    queryFn: () => ingestionService.getPreview(fileId),
    retry: false,
  });

  // Derivar entity_type desde el inferred_type del archivo (no de los checkboxes).
  // Los checkboxes controlan qué importar, no el tipo de schema para mapear.
  const summary = preview?.parsed_summary_json as Record<string, unknown> | null | undefined;
  const _inferredType = typeof summary?.inferred_type === "string" ? summary.inferred_type : "";
  const INFERRED_TO_ENTITY: Record<string, string> = {
    ventas: "sale",
    gastos: "expense",
    stock: "product",
  };
  const entityType = INFERRED_TO_ENTITY[_inferredType] ?? "sale";

  const { data: suggestions = [], isLoading: loadingSuggestions } = useQuery({
    queryKey: ["column-mappings", fileId, entityType],
    queryFn: () => ingestionService.getColumnMappings(fileId, entityType),
    enabled: !!fileId && !!preview,  // esperar a tener el preview para conocer entityType
  });

  // Inicializar mappings desde sugerencias cuando cargan
  if (suggestions.length > 0 && !initialized) {
    const initial: Record<string, string> = {};
    for (const s of suggestions) {
      if (s.target_field) initial[s.source_column] = s.target_field;
    }
    setMappings(initial);
    setInitialized(true);
  }

  const confirmMutation = useMutation({
    mutationFn: (columnMappings: ColumnMapping[]) =>
      ingestionService.confirmFile(fileId, confirmedFields, columnMappings),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
      void queryClient.invalidateQueries({ queryKey: ["column-mappings-learned"] });
      onDone();
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => ingestionService.cancelFile(fileId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
      onDone();
    },
  });

  function getMappingForColumn(col: string): string {
    return mappings[col] ?? "";
  }

  function setMappingForColumn(col: string, target: string) {
    setMappings((prev) => ({ ...prev, [col]: target }));
  }

  // Calcula el status efectivo de una columna:
  // Si hay mapeo local → mapped/unmapped/ignore según el valor.
  // Si no hay mapeo local → usa el status del backend (captura required_missing).
  function computeEffectiveStatus(
    s: ColumnMappingSuggestion,
    currentTarget: string,
  ): ColumnMappingSuggestion["status"] {
    if (currentTarget === "ignore") return "unmapped";
    if (currentTarget) return "mapped";
    return s.status; // "unmapped" | "required_missing" del backend
  }

  function getUnmappedColumns(): string[] {
    return suggestions
      .filter((s) => !mappings[s.source_column] || mappings[s.source_column] === "")
      .map((s) => s.source_column);
  }

  const hasRequiredMissing = suggestions.some(
    (s) => computeEffectiveStatus(s, getMappingForColumn(s.source_column)) === "required_missing",
  );

  function handleConfirmClick() {
    const unmapped = getUnmappedColumns();
    if (unmapped.length > 0) {
      setUnmappedQueue(unmapped);
      setShowUnmappedModal(true);
    } else {
      doConfirm(mappings);
    }
  }

  function handleUnmappedResolve(target: string) {
    const current = unmappedQueue[0];
    const updatedMappings = current ? { ...mappings, [current]: target } : { ...mappings };
    if (current) {
      setMappings(updatedMappings);
    }
    const remaining = unmappedQueue.slice(1);
    if (remaining.length > 0) {
      setUnmappedQueue(remaining);
    } else {
      setShowUnmappedModal(false);
      setUnmappedQueue([]);
      doConfirm(updatedMappings);
    }
  }

  function doConfirm(currentMappings: Record<string, string>) {
    const columnMappings: ColumnMapping[] = Object.entries(currentMappings)
      .filter(([, target]) => Boolean(target))
      .map(([src, target]) => ({ source_column: src, target_field: target }));
    confirmMutation.mutate(columnMappings);
  }

  const rowCount =
    typeof summary?.rows_processed === "number" ? summary.rows_processed : null;
  const colCount = suggestions.length;
  const unmappedCount = getUnmappedColumns().length;
  const fields = CANONICAL_FIELDS[entityType] ?? [];

  // Preview rows para la tabla secundaria
  const previewRows = (
    (summary?.preview_rows as Record<string, unknown>[] | undefined) ??
    (summary?.ventas_detectadas as Record<string, unknown>[] | undefined) ??
    []
  ).slice(0, 10);

  const allHeaders = suggestions.map((s) => s.source_column);

  return (
    <div className="ml-2 mt-3 rounded-xl border border-vk-border-w bg-vk-surface-w p-4">
      {/* Header */}
      <div className="mb-4 flex items-center justify-between gap-4">
        <div>
          <p className="text-sm font-semibold text-vk-text-primary">Mapeo de columnas</p>
          <p className="mt-0.5 text-xs text-vk-text-muted">
            {rowCount != null ? `${rowCount} filas · ` : ""}
            {colCount} columna{colCount !== 1 ? "s" : ""}
            {unmappedCount > 0 && (
              <span className="ml-1 font-medium text-vk-warning">
                · {unmappedCount} sin mapear
              </span>
            )}
          </p>
        </div>
        {/* Selector de tipo (ventas/gastos/productos) */}
        <div className="flex gap-2">
          {(["ventas", "gastos", "productos"] as const).map((key) => (
            <label key={key} className="flex cursor-pointer items-center gap-1.5">
              <input
                type="checkbox"
                checked={confirmedFields[key]}
                onChange={(e) => {
                  setConfirmedFields((prev) => ({ ...prev, [key]: e.target.checked }));
                  setInitialized(false);
                  setMappings({});
                }}
                className="h-3.5 w-3.5 rounded border-vk-border-w accent-vk-blue"
              />
              <span className="text-xs capitalize text-vk-text-secondary">{key}</span>
            </label>
          ))}
        </div>
      </div>

      {loadingSuggestions ? (
        <div className="flex items-center gap-2 py-4 text-xs text-vk-text-muted">
          <div className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-vk-border-w border-t-vk-blue" />
          Analizando columnas...
        </div>
      ) : (
        <>
          {/* Tabla de mapeo — dos paneles */}
          <div className="mb-4 overflow-hidden rounded-lg border border-vk-border-w">
            {/* Headers */}
            <div className="grid grid-cols-2 border-b border-vk-border-w bg-vk-bg-light px-3 py-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-vk-text-muted">
                Columna del archivo
              </span>
              <span className="text-xs font-semibold uppercase tracking-wide text-vk-text-muted">
                Campo en Véktor ({ENTITY_TYPE_LABELS[entityType] ?? entityType})
              </span>
            </div>

            {/* Filas de mapeo */}
            {suggestions.map((s, idx) => {
              const currentTarget = getMappingForColumn(s.source_column);
              const effectiveStatus = computeEffectiveStatus(s, currentTarget);
              const isMapped = effectiveStatus === "mapped";
              const isIgnored = currentTarget === "ignore";
              const isCustom = currentTarget.startsWith("custom_field:");

              return (
                <div
                  key={s.source_column}
                  className={`grid grid-cols-2 gap-0 border-b border-vk-border-w/60 last:border-0 ${
                    effectiveStatus === "required_missing" ? "bg-vk-danger/5" :
                    idx % 2 === 0 ? "bg-vk-surface-w" : "bg-vk-bg-light/40"
                  }`}
                >
                  {/* Panel izquierdo: columna del archivo */}
                  <div className="flex flex-col justify-center border-r border-vk-border-w/60 px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      <StatusDot status={effectiveStatus} />
                      <span className="font-mono text-xs font-medium text-vk-text-primary">
                        {s.source_column}
                      </span>
                    </div>
                    {s.sample_values.length > 0 && (
                      <div className="mt-1 space-y-0.5 pl-4">
                        {s.sample_values.slice(0, 3).map((v, i) => (
                          <p key={i} className="truncate text-[11px] text-vk-text-muted">
                            {v}
                          </p>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Panel derecho: campo destino */}
                  <div className="flex flex-col justify-center px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      {(isMapped || isIgnored) && !isCustom && (
                        <ArrowRight className="h-3 w-3 shrink-0 text-vk-text-muted" />
                      )}
                      <select
                        value={currentTarget}
                        onChange={(e) => setMappingForColumn(s.source_column, e.target.value)}
                        className="w-full rounded border border-vk-border-w bg-vk-bg-light px-2 py-1 text-xs text-vk-text-primary focus:border-vk-blue focus:outline-none"
                      >
                        <option value="">Sin mapear</option>
                        <option value="ignore">— Ignorar columna —</option>
                        {fields.map((f) => (
                          <option key={f.value} value={f.value}>
                            {f.label}
                          </option>
                        ))}
                        {isCustom && (
                          <option value={currentTarget}>{currentTarget}</option>
                        )}
                      </select>
                    </div>
                    {isMapped && s.source !== "none" && (
                      <p className="mt-0.5 pl-5 text-[10px] text-vk-text-muted">
                        {SOURCE_LABELS[s.source] ?? s.source} · {Math.round(s.confidence * 100)}%
                      </p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Preview secundaria: todas las columnas, scroll horizontal */}
          {previewRows.length > 0 && allHeaders.length > 0 && (
            <details className="mb-4">
              <summary className="cursor-pointer text-xs text-vk-text-muted hover:text-vk-text-secondary">
                Ver preview de datos ({previewRows.length} filas)
              </summary>
              <div className="mt-2 overflow-x-auto rounded border border-vk-border-w">
                <table className="w-full whitespace-nowrap text-xs">
                  <thead>
                    <tr className="border-b border-vk-border-w bg-vk-bg-light">
                      {allHeaders.map((h) => (
                        <th
                          key={h}
                          className="px-2 py-1 text-left font-medium text-vk-text-muted"
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {previewRows.map((row, i) => (
                      <tr key={i} className="border-b border-vk-border-w/60">
                        {allHeaders.map((h) => (
                          <td key={h} className="px-2 py-1 text-vk-text-secondary">
                            {String((row as Record<string, unknown>)[h] ?? "—")}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </>
      )}

      {/* Banner de campos requeridos faltantes */}
      {hasRequiredMissing && (
        <div className="mb-2 flex items-center gap-2 rounded-lg border border-vk-danger/30 bg-vk-danger-bg px-3 py-2 text-xs text-vk-danger">
          <XCircle className="h-3.5 w-3.5 shrink-0" />
          Hay campos obligatorios sin mapear (indicados en rojo). Asignales un campo antes de confirmar.
        </div>
      )}

      {/* Error de API */}
      {confirmMutation.isError && (
        <p className="mb-2 text-xs text-vk-danger">
          Error al confirmar. Verificá que los campos requeridos estén mapeados.
        </p>
      )}

      {/* Acciones */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={handleConfirmClick}
          disabled={
            confirmMutation.isPending ||
            !Object.values(confirmedFields).some(Boolean) ||
            hasRequiredMissing
          }
          className="flex items-center gap-1.5 rounded-lg bg-vk-blue px-3 py-1.5 text-xs font-medium text-white hover:bg-vk-blue-hover disabled:opacity-50 transition-colors"
        >
          <CheckCircle className="h-3.5 w-3.5" />
          {confirmMutation.isPending
            ? "Confirmando..."
            : unmappedCount > 0
              ? `Confirmar (${unmappedCount} sin mapear)`
              : "Confirmar importación"}
        </button>
        <button
          type="button"
          onClick={() => cancelMutation.mutate()}
          disabled={cancelMutation.isPending}
          className="rounded-lg border border-vk-border-w px-3 py-1.5 text-xs text-vk-text-secondary hover:bg-vk-bg-light disabled:opacity-50 transition-colors"
        >
          Cancelar
        </button>
      </div>

      {/* Modal para columnas sin mapear */}
      {showUnmappedModal && unmappedQueue.length > 0 && (
        <UnmappedModal
          column={unmappedQueue[0]!}
          entityType={entityType}
          onResolve={handleUnmappedResolve}
          onClose={() => {
            setShowUnmappedModal(false);
            setUnmappedQueue([]);
          }}
        />
      )}
    </div>
  );
}
