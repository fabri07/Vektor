"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle, AlertCircle, XCircle, ArrowRight } from "lucide-react";
import {
  ingestionService,
  type ColumnAtRisk,
  type ColumnMapping,
  type ColumnMappingSuggestion,
  type MappingContext,
  type StockTreatment,
} from "@/services/ingestion.service";
import { StockTreatmentChoice, summaryHasStock } from "./stockTreatment";
import { useToastStore } from "@/stores/toastStore";

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
    { value: "barcode", label: "Código de barras (EAN/UPC)" },
    { value: "name", label: "Nombre" },
    { value: "sale_price_ars", label: "Precio de venta" },
    { value: "unit_cost_ars", label: "Costo unitario" },
    { value: "stock_units", label: "Stock (unidades)" },
    { value: "category", label: "Categoría" },
    { value: "description", label: "Descripción" },
    // F6-B1: espeja CANONICAL_FIELDS["product"] del backend (mantener en sync).
    { value: "acquired_at", label: "Fecha de alta/adquisición" },
    { value: "expiry_date", label: "Fecha de vencimiento" },
  ],
  // F7a: espeja CANONICAL_FIELDS["customer"] del backend (mantener en sync).
  // Aditivo — el import/vinculación real queda para 7b/7c.
  customer: [
    { value: "customer_type", label: "Tipo (persona/empresa)" },
    { value: "name", label: "Nombre" },
    { value: "last_name", label: "Apellido" },
    { value: "doc_type", label: "Tipo de documento" },
    { value: "dni", label: "DNI" },
    { value: "cuit", label: "CUIT" },
    { value: "iva_condition", label: "Condición de IVA" },
    { value: "email", label: "Email" },
    { value: "phone", label: "Teléfono" },
    { value: "address", label: "Dirección" },
    { value: "locality", label: "Localidad" },
    { value: "province", label: "Provincia" },
    { value: "postal_code", label: "Código postal" },
    { value: "birthday", label: "Cumpleaños" },
    { value: "notes", label: "Notas" },
  ],
  // F7a: espeja CANONICAL_FIELDS["supplier"] del backend — ACOTADO a lo que
  // persiste el modelo Supplier hoy (sin domicilio/condición IVA).
  supplier: [
    { value: "name", label: "Nombre" },
    { value: "last_name", label: "Apellido" },
    { value: "cuil", label: "CUIL" },
    { value: "payment_method", label: "Método de pago" },
    { value: "email", label: "Email" },
    { value: "phone", label: "Teléfono" },
    { value: "notes", label: "Notas" },
  ],
};

const ENTITY_TYPE_LABELS: Record<string, string> = {
  sale: "Ventas",
  expense: "Gastos",
  product: "Productos",
  customer: "Clientes",
  supplier: "Proveedores",
};

const SOURCE_LABELS: Record<string, string> = {
  tenant_history: "Historial",
  heuristic: "Heurística",
  fuzzy: "Similar",
  llm: "IA",
  none: "—",
};

// F4: un error de confirm que NO es de mapeo — 409 (confirm concurrente / archivo
// ya importado o importándose) o timeout del cliente (el import sigue en curso en
// el backend). No debe pintar el banner rojo de "revisá los campos".
function isTransientConfirmError(error: unknown): boolean {
  const e = error as { response?: { status?: number }; code?: string } | null;
  return e?.response?.status === 409 || e?.code === "ECONNABORTED";
}

// Maneja el error transitorio del confirm: avisa amable y refresca la lista.
// Devuelve true si lo manejó (para no caer en el banner de error de mapeo).
function handleTransientConfirmError(
  error: unknown,
  toast: (msg: string, variant: "warning" | "info") => void,
  invalidate: () => void,
): boolean {
  const e = error as { response?: { status?: number }; code?: string } | null;
  if (e?.response?.status === 409) {
    toast(
      "Ya se está importando este archivo o ya se importó — actualizá la lista",
      "warning",
    );
    invalidate();
    return true;
  }
  if (e?.code === "ECONNABORTED") {
    // Timeout del cliente: el import sigue corriendo en el backend. No es un error.
    toast(
      "El import sigue en curso — revisá la lista en unos segundos",
      "info",
    );
    invalidate();
    return true;
  }
  return false;
}

// FASE 2 (A2)/A3: color de la confianza del mapeo (verde alto / ámbar medio / rojo bajo).
function confidenceColor(confidence: number): string {
  if (confidence >= 0.75) return "text-vk-success";
  if (confidence >= 0.5) return "text-vk-warning";
  return "text-vk-danger";
}

// A3: lista reutilizable de columnas con muchos datos vacíos (columns_at_risk).
function ColumnsAtRiskList({ items }: { items: ColumnAtRisk[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className="mb-1 text-[11px] text-vk-text-secondary">
        Columnas con muchos datos vacíos:
      </p>
      <ul className="space-y-0.5">
        {items.map((c) => (
          <li key={c.column} className="flex items-center gap-1.5 text-[11px] text-vk-text-muted">
            <XCircle className="h-3 w-3 shrink-0 text-vk-warning" />
            <span className="font-mono text-vk-text-secondary">{c.column}</span>
            <span>· {Math.round(c.null_pct)}% vacío — {c.recommendation}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// Propósitos posibles cuando el tipo del archivo quedó ambiguo ("general").
const PURPOSE_OPTIONS: Array<{ value: string; label: string; field: "ventas" | "gastos" | "productos" }> = [
  { value: "ventas", label: "Ventas", field: "ventas" },
  { value: "gastos", label: "Gastos", field: "gastos" },
  { value: "stock", label: "Productos / Stock", field: "productos" },
];

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

// Status efectivo de una columna: si hay target local → mapped; "ignore" → unmapped;
// si no hay target → status del backend (captura required_missing).
function effectiveStatus(
  s: ColumnMappingSuggestion,
  target: string,
): ColumnMappingSuggestion["status"] {
  if (target === "ignore") return "unmapped";
  if (target) return "mapped";
  return s.status;
}

// ── Mapeo multi-contexto (multi-hoja): una sección por hoja/grupo ──────────────

function SheetMapperSection({
  fileId,
  context,
  included,
  entity,
  onIncludeChange,
  onMappingsChange,
  onEntityChange,
}: {
  fileId: string;
  context: MappingContext;
  included: boolean;
  entity: string;
  onIncludeChange: (ctxId: string, included: boolean) => void;
  onMappingsChange: (ctxId: string, mappings: Record<string, string>) => void;
  onEntityChange: (ctxId: string, entity: string) => void;
}) {
  // Texto/imagen no tiene columnas: se mapea el grupo a un tipo, sin dropdowns.
  const isText = context.headers == null;
  const [mappings, setMappings] = useState<Record<string, string>>({});
  const [initialized, setInitialized] = useState(false);
  // Columna que está creando un custom field + su key en edición.
  const [customFor, setCustomFor] = useState<string | null>(null);
  const [customKey, setCustomKey] = useState("");

  function selectTarget(col: string, value: string) {
    if (value === "__custom__") {
      setCustomFor(col);
      setCustomKey("");
      return;
    }
    setCustomFor((c) => (c === col ? null : c));
    setMappings((p) => ({ ...p, [col]: value }));
  }

  function commitCustom(col: string) {
    const key = customKey.trim().toLowerCase().replace(/\s+/g, "_");
    if (key) setMappings((p) => ({ ...p, [col]: `custom_field:${key}` }));
    setCustomFor(null);
    setCustomKey("");
  }

  const { data: suggestions = [], isLoading } = useQuery({
    queryKey: ["column-mappings", fileId, context.context_id],
    queryFn: () =>
      ingestionService.getColumnMappings(fileId, entity, context.context_id),
    enabled: included && !isText,
  });

  // Inicializar mapeos desde sugerencias (una vez).
  useEffect(() => {
    if (suggestions.length > 0 && !initialized) {
      const initial: Record<string, string> = {};
      for (const s of suggestions) {
        if (s.target_field) initial[s.source_column] = s.target_field;
      }
      setMappings(initial);
      setInitialized(true);
    }
  }, [suggestions, initialized]);

  // Reportar mapeos al padre cuando cambian (vacío en texto/imagen).
  useEffect(() => {
    onMappingsChange(context.context_id, mappings);
  }, [mappings, context.context_id, onMappingsChange]);

  const fields = CANONICAL_FIELDS[entity] ?? [];
  const reqMissing = suggestions.some(
    (s) => effectiveStatus(s, mappings[s.source_column] ?? "") === "required_missing",
  );
  const previewLines = context.preview_rows
    .map((r) => String(r.linea ?? Object.values(r)[0] ?? ""))
    .filter(Boolean)
    .slice(0, 6);

  return (
    <div
      className={`rounded-lg border bg-vk-surface-w ${
        included ? "border-vk-border-w" : "border-vk-border-w/40 opacity-60"
      }`}
    >
      <div className="flex items-center justify-between gap-3 border-b border-vk-border-w bg-vk-bg-light px-3 py-2">
        <label className="flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            checked={included}
            onChange={(e) => onIncludeChange(context.context_id, e.target.checked)}
            className="h-3.5 w-3.5 rounded border-vk-border-w accent-vk-blue"
          />
          <span className="text-sm font-semibold text-vk-text-primary">{context.label}</span>
        </label>
        {isText ? (
          // Reasignable: el clasificador de texto puede equivocarse.
          <select
            value={entity}
            onChange={(e) => onEntityChange(context.context_id, e.target.value)}
            className="rounded border border-vk-border-w bg-vk-bg-light px-2 py-0.5 text-xs text-vk-text-primary focus:border-vk-blue focus:outline-none"
          >
            <option value="sale">Ventas</option>
            <option value="expense">Gastos</option>
            <option value="customer">Clientes</option>
            <option value="supplier">Proveedores</option>
          </select>
        ) : (
          <span className="rounded-full bg-vk-info-bg px-2 py-0.5 text-[10px] font-medium text-vk-blue">
            {ENTITY_TYPE_LABELS[entity] ?? entity} · {context.row_count} fila
            {context.row_count !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      {included &&
        (isText ? (
          <div className="p-2">
            <p className="mb-1 px-1 text-[10px] uppercase tracking-wide text-vk-text-muted">
              {context.row_count} línea{context.row_count !== 1 ? "s" : ""} detectada
              {context.row_count !== 1 ? "s" : ""}
            </p>
            <div className="space-y-0.5">
              {previewLines.map((line, i) => (
                <p
                  key={i}
                  className="truncate rounded bg-vk-bg-light/60 px-2 py-1 text-[11px] text-vk-text-secondary"
                  title={line}
                >
                  {line}
                </p>
              ))}
            </div>
          </div>
        ) : isLoading ? (
          <div className="px-3 py-3 text-xs text-vk-text-muted">Analizando columnas...</div>
        ) : (
          <div className="p-2">
            {reqMissing && (
              <div className="mb-2 flex items-center gap-2 rounded border border-vk-danger/30 bg-vk-danger-bg px-2 py-1 text-[11px] text-vk-danger">
                <XCircle className="h-3 w-3 shrink-0" />
                Campos obligatorios sin mapear (en rojo).
              </div>
            )}
            {suggestions.map((s) => {
              const target = mappings[s.source_column] ?? "";
              const eff = effectiveStatus(s, target);
              const isCustom = target.startsWith("custom_field:");
              return (
                <div
                  key={s.source_column}
                  className={`grid grid-cols-2 items-start gap-2 px-1 py-1.5 ${
                    eff === "required_missing" ? "bg-vk-danger/5" : ""
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-2 pt-1">
                    <StatusDot status={eff} />
                    <span
                      className="truncate font-mono text-xs text-vk-text-primary"
                      title={s.source_column}
                    >
                      {s.source_column}
                    </span>
                  </div>
                  <div className="flex flex-col gap-1">
                    <select
                      value={customFor === s.source_column ? "__custom__" : target}
                      onChange={(e) => selectTarget(s.source_column, e.target.value)}
                      className="w-full rounded border border-vk-border-w bg-vk-bg-light px-2 py-1 text-xs text-vk-text-primary focus:border-vk-blue focus:outline-none"
                    >
                      <option value="">Sin mapear</option>
                      <option value="ignore">— Ignorar —</option>
                      {fields.map((f) => (
                        <option key={f.value} value={f.value}>
                          {f.label}
                        </option>
                      ))}
                      {isCustom && <option value={target}>{target}</option>}
                      <option value="__custom__">+ Campo personalizado…</option>
                    </select>
                    {customFor === s.source_column && (
                      <div className="flex gap-1">
                        <input
                          type="text"
                          autoFocus
                          value={customKey}
                          onChange={(e) => setCustomKey(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") commitCustom(s.source_column);
                          }}
                          placeholder="nombre_del_campo"
                          className="w-full rounded border border-vk-border-w bg-vk-bg-light px-2 py-1 text-[11px] text-vk-text-primary placeholder:text-vk-text-muted focus:border-vk-blue focus:outline-none"
                        />
                        <button
                          type="button"
                          onClick={() => commitCustom(s.source_column)}
                          disabled={!customKey.trim()}
                          className="shrink-0 rounded bg-vk-blue px-2 py-1 text-[11px] font-medium text-white hover:bg-vk-blue-hover disabled:opacity-50"
                        >
                          OK
                        </button>
                      </div>
                    )}
                    {/* A3: source + confidence (mismo criterio que el flujo single). */}
                    {eff === "mapped" && s.source !== "none" && (
                      <p className="text-[10px] text-vk-text-muted">
                        {SOURCE_LABELS[s.source] ?? s.source} ·{" "}
                        <span className={confidenceColor(s.confidence)}>
                          {Math.round(s.confidence * 100)}%
                        </span>
                      </p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        ))}
    </div>
  );
}

function MultiContextMapper({
  fileId,
  contexts,
  columnsAtRisk,
  onDone,
}: {
  fileId: string;
  contexts: MappingContext[];
  columnsAtRisk: ColumnAtRisk[];
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);
  const [included, setIncluded] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(contexts.map((c) => [c.context_id, true])),
  );
  // entity_type efectivo por contexto (reasignable en texto/imagen).
  const [entityByCtx, setEntityByCtx] = useState<Record<string, string>>(() =>
    Object.fromEntries(contexts.map((c) => [c.context_id, c.entity_type ?? "sale"])),
  );
  // A: tratamiento del stock si alguna hoja incluida es de productos.
  const [stockTreatment, setStockTreatment] =
    useState<StockTreatment>("opening_balance");
  const mappingsRef = useRef<Record<string, Record<string, string>>>({});

  const handleIncludeChange = useCallback((ctxId: string, inc: boolean) => {
    setIncluded((prev) => ({ ...prev, [ctxId]: inc }));
  }, []);

  const handleEntityChange = useCallback((ctxId: string, ent: string) => {
    setEntityByCtx((prev) => ({ ...prev, [ctxId]: ent }));
  }, []);

  const handleMappingsChange = useCallback(
    (ctxId: string, mappings: Record<string, string>) => {
      mappingsRef.current[ctxId] = mappings;
    },
    [],
  );

  // A: alguna hoja incluida es de productos → preguntar cómo tratar el stock.
  const hasStock = contexts.some(
    (c) =>
      included[c.context_id] &&
      (entityByCtx[c.context_id] ?? c.entity_type) === "product",
  );

  const confirmMutation = useMutation({
    mutationFn: () => {
      const columnMappings: ColumnMapping[] = [];
      const contextEntity: Record<string, string> = {};
      for (const ctx of contexts) {
        if (!included[ctx.context_id]) continue;
        const ent = entityByCtx[ctx.context_id] ?? ctx.entity_type ?? "sale";
        // Override de tipo solo para texto/imagen (sin headers).
        if (ctx.headers == null) contextEntity[ctx.context_id] = ent;
        const m = mappingsRef.current[ctx.context_id] ?? {};
        for (const [src, target] of Object.entries(m)) {
          if (!target) continue;
          columnMappings.push({
            source_column: src,
            target_field: target,
            context_id: ctx.context_id,
            entity_type: ent,
          });
        }
      }
      return ingestionService.confirmFile(
        fileId,
        {},
        columnMappings,
        included,
        contextEntity,
        hasStock ? stockTreatment : undefined,
      );
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
      void queryClient.invalidateQueries({ queryKey: ["column-mappings-learned"] });
      // Avisos human-in-the-loop: el panel se cierra en onDone(), así que van como
      // toasts (compras sin proveedor/producto, filas a "Otros").
      for (const w of result.warnings ?? []) toast(w, "warning");
      onDone();
    },
    onError: (error) => {
      // 409 (concurrente/ya importado) o timeout (import en curso): no es un error
      // de mapeo — se avisa amable y se refresca la lista, sin banner rojo.
      handleTransientConfirmError(error, toast, () =>
        queryClient.invalidateQueries({ queryKey: ["ingestion-files"] }),
      );
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => ingestionService.cancelFile(fileId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
      onDone();
    },
  });

  const anyIncluded = Object.values(included).some(Boolean);
  const errDetail = (
    confirmMutation.error as { response?: { data?: { detail?: string } } } | null
  )?.response?.data?.detail;

  return (
    <div className="ml-2 mt-3 rounded-xl border border-vk-border-w bg-vk-surface-w p-4">
      <div className="mb-3">
        <p className="text-sm font-semibold text-vk-text-primary">Mapeo por hoja</p>
        <p className="mt-0.5 text-xs text-vk-text-muted">
          {contexts.length} hojas detectadas. Revisá el mapeo de cada una y elegí cuáles importar.
        </p>
      </div>

      {/* A3: bloque "Revisá antes de confirmar" — columns_at_risk también en multi-hoja. */}
      {columnsAtRisk.length > 0 && (
        <div className="mb-3 rounded-lg border border-vk-warning/30 bg-vk-warning-bg/40 p-3">
          <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-vk-text-primary">
            <AlertCircle className="h-3.5 w-3.5 shrink-0 text-vk-warning" />
            Revisá antes de confirmar
          </p>
          <ColumnsAtRiskList items={columnsAtRisk} />
        </div>
      )}

      <div className="space-y-3">
        {contexts.map((ctx) => (
          <SheetMapperSection
            key={ctx.context_id}
            fileId={fileId}
            context={ctx}
            included={included[ctx.context_id] ?? false}
            entity={entityByCtx[ctx.context_id] ?? ctx.entity_type ?? "sale"}
            onIncludeChange={handleIncludeChange}
            onMappingsChange={handleMappingsChange}
            onEntityChange={handleEntityChange}
          />
        ))}
      </div>

      {/* A: tratamiento del stock cuando alguna hoja incluida es de productos. */}
      {hasStock && (
        <StockTreatmentChoice
          value={stockTreatment}
          onChange={setStockTreatment}
          className="mt-4 rounded-lg border border-vk-border-w bg-vk-bg-light/40 p-3"
        />
      )}

      {confirmMutation.isError &&
        !isTransientConfirmError(confirmMutation.error) && (
          <p className="mt-3 text-xs text-vk-danger">
            {errDetail ?? "Error al confirmar. Revisá los campos requeridos de cada hoja."}
          </p>
        )}

      <div className="mt-4 flex items-center gap-2">
        <button
          type="button"
          onClick={() => confirmMutation.mutate()}
          disabled={confirmMutation.isPending || !anyIncluded}
          className="flex items-center gap-1.5 rounded-lg bg-vk-blue px-3 py-1.5 text-xs font-medium text-white hover:bg-vk-blue-hover disabled:opacity-50 transition-colors"
        >
          <CheckCircle className="h-3.5 w-3.5" />
          {confirmMutation.isPending ? "Confirmando..." : "Confirmar importación"}
        </button>
        <button
          type="button"
          onClick={() => cancelMutation.mutate()}
          disabled={cancelMutation.isPending || confirmMutation.isPending}
          className="rounded-lg border border-vk-border-w px-3 py-1.5 text-xs text-vk-text-secondary hover:bg-vk-bg-light disabled:opacity-50 transition-colors"
        >
          Cancelar
        </button>
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
  const toast = useToastStore((s) => s.add);
  const [confirmedFields, setConfirmedFields] = useState({
    ventas: true,
    gastos: false,
    productos: false,
  });
  const [mappings, setMappings] = useState<Record<string, string>>({});
  const [unmappedQueue, setUnmappedQueue] = useState<string[]>([]);
  const [showUnmappedModal, setShowUnmappedModal] = useState(false);
  const [initialized, setInitialized] = useState(false);
  // A3: cuando el tipo quedó ambiguo ("general"), el usuario elige el propósito aquí.
  const [purpose, setPurpose] = useState<string>("");
  // A: tratamiento del stock cuando el archivo trae productos (default: saldo de apertura).
  const [stockTreatment, setStockTreatment] =
    useState<StockTreatment>("opening_balance");

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
  // A3: archivo ambiguo ("general") → el tipo efectivo es el propósito elegido.
  const isAmbiguous = _inferredType === "general";
  const effectiveInferred = isAmbiguous ? purpose : _inferredType;
  const needsPurpose = isAmbiguous && !purpose;
  const entityType = INFERRED_TO_ENTITY[effectiveInferred] ?? "sale";
  // columns_at_risk (Sprint 14): hasta ahora no se renderizaban en el panel.
  const columnsAtRisk = preview?.columns_at_risk ?? [];
  // A: el archivo trae stock si el schema efectivo es de productos, si se tildó
  // "productos", o si el summary detectó stock/productos.
  const hasStock =
    entityType === "product" ||
    confirmedFields.productos ||
    summaryHasStock(summary);

  function choosePurpose(value: string) {
    setPurpose(value);
    const opt = PURPOSE_OPTIONS.find((o) => o.value === value);
    if (opt) {
      setConfirmedFields({ ventas: false, gastos: false, productos: false, [opt.field]: true });
    }
    // Re-inicializar el mapeo con el nuevo schema de entidad.
    setInitialized(false);
    setMappings({});
  }

  // Multi-contexto: se usa el mapeo por contexto cuando hay >1 contexto (multi-hoja)
  // o cuando el único contexto NO es una tabla (texto/imagen). Una sola tabla
  // (csv / xlsx 1 hoja) sigue por el flujo legacy (mapeo plano → insert single-sheet).
  const contexts = (
    Array.isArray(summary?.mapping_contexts) ? summary.mapping_contexts : []
  ) as MappingContext[];
  const isMultiContext =
    contexts.length > 1 ||
    (contexts.length === 1 && contexts[0]?.source_kind !== "table");

  const { data: suggestions = [], isLoading: loadingSuggestions } = useQuery({
    queryKey: ["column-mappings", fileId, entityType],
    queryFn: () => ingestionService.getColumnMappings(fileId, entityType),
    // Esperar al preview para conocer entityType; no correr en multi-hoja (cada
    // sección maneja su propia query) ni hasta que se elija propósito si es ambiguo.
    enabled: !!fileId && !!preview && !isMultiContext && !needsPurpose,
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
      ingestionService.confirmFile(
        fileId,
        confirmedFields,
        columnMappings,
        undefined,
        undefined,
        hasStock ? stockTreatment : undefined,
      ),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["ingestion-files"] });
      void queryClient.invalidateQueries({ queryKey: ["column-mappings-learned"] });
      // Avisos human-in-the-loop: el panel se cierra en onDone(), así que van como
      // toasts (compras sin proveedor/producto, filas a "Otros").
      for (const w of result.warnings ?? []) toast(w, "warning");
      onDone();
    },
    onError: (error) => {
      // 409 (concurrente/ya importado) o timeout (import en curso): no es un error
      // de mapeo — se avisa amable y se refresca la lista, sin banner rojo.
      handleTransientConfirmError(error, toast, () =>
        queryClient.invalidateQueries({ queryKey: ["ingestion-files"] }),
      );
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

  // A3: columnas que merecen revisión antes de confirmar — requeridas sin mapear,
  // sin mapear, o mapeadas con baja confianza (<50%). Las ignoradas a propósito no.
  const needsAttention = suggestions.filter((s) => {
    const t = getMappingForColumn(s.source_column);
    if (t === "ignore") return false;
    const eff = computeEffectiveStatus(s, t);
    if (eff === "required_missing" || eff === "unmapped") return true;
    return eff === "mapped" && s.confidence < 0.5;
  });

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

  // Multi-hoja: delegar al mapeo por contexto (todos los hooks ya se ejecutaron).
  if (isMultiContext) {
    return (
      <MultiContextMapper
        fileId={fileId}
        contexts={contexts}
        columnsAtRisk={columnsAtRisk}
        onDone={onDone}
      />
    );
  }

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
        {/* Selector de tipo (ventas/gastos/productos). En archivos ambiguos lo
            reemplaza el selector de propósito del bloque "Revisá antes de confirmar". */}
        <div className={`flex gap-2 ${isAmbiguous ? "hidden" : ""}`}>
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

      {/* A3: bloque consolidado "Revisá antes de confirmar" */}
      {(isAmbiguous || columnsAtRisk.length > 0 || needsAttention.length > 0) && (
        <div className="mb-4 rounded-lg border border-vk-warning/30 bg-vk-warning-bg/40 p-3">
          <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-vk-text-primary">
            <AlertCircle className="h-3.5 w-3.5 shrink-0 text-vk-warning" />
            Revisá antes de confirmar
          </p>

          {/* Selector de propósito cuando el tipo es ambiguo ("general") */}
          {isAmbiguous && (
            <div className="mb-3">
              <p className="mb-1 text-[11px] text-vk-text-secondary">
                No pudimos determinar si este archivo es de ventas, gastos o productos.
                Elegí el tipo:
              </p>
              <div className="flex flex-wrap gap-1.5">
                {PURPOSE_OPTIONS.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => choosePurpose(opt.value)}
                    className={`rounded-lg border px-3 py-1 text-xs transition-colors ${
                      purpose === opt.value
                        ? "border-vk-blue bg-vk-info-bg text-vk-blue"
                        : "border-vk-border-w text-vk-text-secondary hover:bg-vk-bg-light"
                    }`}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Columnas con muchos datos vacíos (columns_at_risk, Sprint 14) */}
          {columnsAtRisk.length > 0 && (
            <div className="mb-3">
              <ColumnsAtRiskList items={columnsAtRisk} />
            </div>
          )}

          {/* Columnas que necesitan atención, editables inline */}
          {needsAttention.length > 0 && (
            <div>
              <p className="mb-1 text-[11px] text-vk-text-secondary">
                Columnas a revisar ({needsAttention.length}):
              </p>
              <div className="space-y-1">
                {needsAttention.map((s) => {
                  const target = getMappingForColumn(s.source_column);
                  const eff = computeEffectiveStatus(s, target);
                  return (
                    <div key={s.source_column} className="flex items-center gap-2">
                      <StatusDot status={eff} />
                      <span
                        className="w-32 shrink-0 truncate font-mono text-[11px] text-vk-text-primary"
                        title={s.source_column}
                      >
                        {s.source_column}
                      </span>
                      <select
                        value={target}
                        onChange={(e) => setMappingForColumn(s.source_column, e.target.value)}
                        className="min-w-0 flex-1 rounded border border-vk-border-w bg-vk-bg-light px-2 py-1 text-[11px] text-vk-text-primary focus:border-vk-blue focus:outline-none"
                      >
                        <option value="">Sin mapear</option>
                        <option value="ignore">— Ignorar —</option>
                        {fields.map((f) => (
                          <option key={f.value} value={f.value}>
                            {f.label}
                          </option>
                        ))}
                      </select>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {needsPurpose ? (
        <div className="py-4 text-xs text-vk-text-muted">
          Elegí el tipo de archivo arriba para ver el mapeo de columnas.
        </div>
      ) : loadingSuggestions ? (
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
                        {SOURCE_LABELS[s.source] ?? s.source} ·{" "}
                        <span className={confidenceColor(s.confidence)}>
                          {Math.round(s.confidence * 100)}%
                        </span>
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

      {/* A: tratamiento del stock cuando el archivo trae productos. */}
      {hasStock && !needsPurpose && (
        <StockTreatmentChoice
          value={stockTreatment}
          onChange={setStockTreatment}
          className="mb-3 rounded-lg border border-vk-border-w bg-vk-bg-light/40 p-3"
        />
      )}

      {/* Error de API (excluye 409/timeout: los maneja el toast, no el banner) */}
      {confirmMutation.isError &&
        !isTransientConfirmError(confirmMutation.error) && (
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
            hasRequiredMissing ||
            needsPurpose
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
          disabled={cancelMutation.isPending || confirmMutation.isPending}
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
