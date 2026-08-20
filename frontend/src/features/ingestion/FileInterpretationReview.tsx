"use client";

import { useCallback, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2, RefreshCw } from "lucide-react";

import { ingestionService } from "@/services/ingestion.service";
import type {
  ColumnMapping,
  ColumnRiskDecision,
  ContextualColumnRisk,
  MappingContext,
  RereadImpactProjection,
  RereadPreviewResponse,
  RereadSheetStatus,
  StockTreatment,
} from "@/services/ingestion.service";

import { ColumnMappingTable } from "./ColumnMappingTable";
import { ColumnRiskDecisionsPanel } from "./ColumnRiskDecisionsPanel";
import { DataSample } from "./DataSample";
import { ImportImpactSummary } from "./ImportImpactSummary";
import { SheetNavigator } from "./SheetNavigator";
import { StockTreatmentChoice } from "./stockTreatment";

// Corrección C3 (revisión externa 2026-08-19): mismas 5 secciones reales que
// el flujo de carga inicial (`ColumnMapperPanel`) ofrece para reasignar una
// hoja — duplicado a propósito, sin importar desde ese archivo (2508 líneas,
// camino de carga inicial en producción; ver decisión de alcance del plan).
const ENTITY_OPTIONS = ["sale", "expense", "product", "customer", "supplier"] as const;
const ENTITY_TYPE_LABELS: Record<string, string> = {
  sale: "Ventas",
  expense: "Gastos",
  product: "Productos",
  customer: "Clientes",
  supplier: "Proveedores",
};

/** Catálogo de campos: estático por deploy, react-query lo dedupea por
 * `queryKey` con la copia de `ColumnMapperPanel` (misma key, misma cache). */
function useFieldCatalog() {
  return useQuery({
    queryKey: ["ingestion-field-catalog"],
    queryFn: () => ingestionService.getFieldCatalog(),
    staleTime: Infinity,
  });
}

/**
 * F-RR Fase 8: revisión completa de interpretación de una sesión de
 * relectura — hojas, sección efectiva, columnas/mapeos, riesgo, filas de
 * ejemplo, impacto proyectado, y la posibilidad de corregir todo eso antes
 * de aplicar. Reemplaza los contadores planos que mostraba el modal viejo.
 *
 * Permite corregir el mapeo columna→campo, las decisiones de riesgo por
 * hoja, reasignar la entidad de una hoja completa, incluir/excluirla de la
 * relectura y elegir el tratamiento de stock de una hoja de productos
 * (corrección C3, revisión externa 2026-08-19 — el plan original de esta
 * fase dejaba estos tres ejes para una iteración siguiente).
 */
export function FileInterpretationReview({
  fileId,
  runId,
  sheets,
  mappingContexts,
  contextualColumnRisk,
  impact,
  onPreviewUpdated,
  onError,
}: {
  fileId: string;
  runId: string;
  sheets: RereadSheetStatus[];
  mappingContexts: MappingContext[];
  contextualColumnRisk: ContextualColumnRisk[];
  impact: RereadImpactProjection;
  onPreviewUpdated: (preview: RereadPreviewResponse) => void;
  onError: (message: string) => void;
}) {
  const [activeContextId, setActiveContextId] = useState<string | null>(
    sheets[0]?.context_id ?? null,
  );
  // Correcciones del usuario, por contexto: source_column -> target_field.
  // Solo lo que el usuario TOCÓ — lo que no tocó sigue la sugerencia del
  // backend (mismo criterio que el confirm: el borrador son deltas, no una
  // redeclaración completa del mapeo).
  const [overrides, setOverrides] = useState<Record<string, Record<string, string>>>({});
  const [riskDecisions, setRiskDecisions] = useState<ColumnRiskDecision[]>([]);
  // Corrección C3: reasignar la entidad de una hoja completa, incluir/
  // excluirla, y el tratamiento de stock por hoja de productos — solo lo que
  // el usuario TOCÓ, mismo criterio que `overrides` (columna↔campo).
  const [entityOverrides, setEntityOverrides] = useState<Record<string, string>>({});
  const [contextConfirmedOverrides, setContextConfirmedOverrides] = useState<
    Record<string, boolean>
  >({});
  const [stockTreatmentOverrides, setStockTreatmentOverrides] = useState<
    Record<string, StockTreatment>
  >({});
  const [updating, setUpdating] = useState(false);

  const activeSheet = sheets.find((s) => s.context_id === activeContextId) ?? null;
  // Entidad EFECTIVA de la hoja activa: la reasignación del usuario le gana a
  // la que trae el preview — mismo criterio de prioridad que el resto del
  // borrador (draft explícito > lo detectado).
  const activeEntityType =
    (activeContextId && entityOverrides[activeContextId]) || activeSheet?.entity_type || "sale";
  const catalogQuery = useFieldCatalog();
  const fields = useMemo(
    () => catalogQuery.data?.[activeEntityType]?.fields ?? [],
    [catalogQuery.data, activeEntityType],
  );

  const suggestionsQuery = useQuery({
    queryKey: ["reread-column-mappings", fileId, runId, activeContextId, activeEntityType],
    queryFn: () =>
      ingestionService.getColumnMappings(fileId, activeEntityType, activeContextId ?? undefined, runId),
    enabled: !!activeContextId && !!activeSheet,
  });

  // Confirmado por sección SIEMPRE completo (todas las hojas, no solo la
  // visitada) — si viniera parcial, `context_is_included` (backend) trataría
  // cualquier hoja ausente como excluida en cuanto el dict deja de estar vacío.
  // La exclusión explícita del usuario (checkbox "incluir esta hoja") le gana
  // al status derivado del backend.
  const contextConfirmed = useMemo(() => {
    const base = Object.fromEntries(sheets.map((s) => [s.context_id, s.status !== "ignorada"]));
    return { ...base, ...contextConfirmedOverrides };
  }, [sheets, contextConfirmedOverrides]);

  const flattenedMappings = useMemo((): ColumnMapping[] => {
    const out: ColumnMapping[] = [];
    for (const [contextId, cols] of Object.entries(overrides)) {
      for (const [sourceColumn, targetField] of Object.entries(cols)) {
        out.push({
          source_column: sourceColumn,
          target_field: targetField,
          context_id: contextId,
          user_selected: true,
        });
      }
    }
    return out;
  }, [overrides]);

  const handleMappingChange = useCallback(
    (sourceColumn: string, target: string) => {
      if (!activeContextId) return;
      setOverrides((prev) => ({
        ...prev,
        [activeContextId]: { ...(prev[activeContextId] ?? {}), [sourceColumn]: target },
      }));
    },
    [activeContextId],
  );

  const recomputeRisk = useCallback(
    (signal: AbortSignal) =>
      ingestionService.recomputeColumnRisk(
        fileId,
        {
          columnMappings: flattenedMappings,
          contextEntity: entityOverrides,
          confirmedFields: {},
          contextConfirmed,
          rereadRunId: runId,
        },
        signal,
      ),
    [fileId, flattenedMappings, entityOverrides, contextConfirmed, runId],
  );

  const hasChanges =
    flattenedMappings.length > 0 ||
    riskDecisions.length > 0 ||
    Object.keys(entityOverrides).length > 0 ||
    Object.keys(contextConfirmedOverrides).length > 0 ||
    Object.keys(stockTreatmentOverrides).length > 0;

  async function handleUpdatePreview() {
    setUpdating(true);
    try {
      const preview = await ingestionService.rereadPreview(fileId, {
        columnMappings: flattenedMappings,
        contextEntity: entityOverrides,
        confirmedFields: {},
        contextConfirmed,
        columnRiskDecisions: riskDecisions,
        stockTreatment: Object.keys(stockTreatmentOverrides).length
          ? stockTreatmentOverrides
          : undefined,
      });
      onPreviewUpdated(preview);
    } catch {
      onError("No se pudo actualizar la vista previa con la corrección.");
    } finally {
      setUpdating(false);
    }
  }

  if (sheets.length === 0) {
    return (
      <p className="text-xs text-vk-text-muted">
        No se detectaron hojas para revisar en este archivo.
      </p>
    );
  }

  return (
    <div className="space-y-3" data-testid="file-interpretation-review">
      <SheetNavigator
        sheets={sheets}
        activeContextId={activeContextId}
        onSelect={setActiveContextId}
      />

      {activeSheet && (
        <div
          className={[
            "rounded-lg border border-vk-border-w bg-vk-surface-w/30 p-3",
            contextConfirmed[activeContextId ?? ""] ? "" : "opacity-60",
          ].join(" ")}
        >
          {/* Corrección C3: reasignar la entidad de la hoja + incluirla o
              excluirla de la relectura — antes solo se podía corregir
              columna↔campo. Mismo patrón visual que el header de hoja del
              flujo de carga inicial (`ColumnMapperPanel`). */}
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-vk-border-w pb-2">
            <label className="flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={contextConfirmed[activeContextId ?? ""] ?? true}
                onChange={(e) => {
                  const cid = activeContextId;
                  if (!cid) return;
                  setContextConfirmedOverrides((prev) => ({
                    ...prev,
                    [cid]: e.target.checked,
                  }));
                }}
                className="h-3.5 w-3.5 rounded border-vk-border-w accent-vk-blue"
              />
              <span className="text-xs font-medium text-vk-text-primary">
                Incluir esta hoja en la relectura
              </span>
            </label>
            <select
              aria-label={`Sección de la hoja ${activeSheet.label}`}
              value={activeEntityType}
              onChange={(e) => {
                const cid = activeContextId;
                if (!cid) return;
                setEntityOverrides((prev) => ({ ...prev, [cid]: e.target.value }));
              }}
              className="rounded border border-vk-border-w bg-vk-bg-light px-2 py-0.5 text-xs text-vk-text-primary focus:border-vk-blue focus:outline-none"
            >
              {ENTITY_OPTIONS.map((value) => (
                <option key={value} value={value}>
                  {ENTITY_TYPE_LABELS[value]}
                </option>
              ))}
            </select>
          </div>

          <ColumnMappingTable
            suggestions={suggestionsQuery.data ?? []}
            fields={fields}
            mapping={overrides[activeContextId ?? ""] ?? {}}
            onMappingChange={handleMappingChange}
            loading={suggestionsQuery.isLoading || catalogQuery.isLoading}
          />

          {/* Solo para hojas de productos — mismo componente que usa la
              carga inicial (`stockTreatment.tsx`, ya compartido). */}
          {activeEntityType === "product" && (
            <StockTreatmentChoice
              value={stockTreatmentOverrides[activeContextId ?? ""] ?? "opening_balance"}
              onChange={(v) => {
                const cid = activeContextId;
                if (!cid) return;
                setStockTreatmentOverrides((prev) => ({ ...prev, [cid]: v }));
              }}
              className="mt-3 border-t border-vk-border-w pt-3"
            />
          )}
        </div>
      )}

      <ColumnRiskDecisionsPanel
        initialRisks={contextualColumnRisk}
        recomputeKey={JSON.stringify({ flattenedMappings, entityOverrides, contextConfirmed })}
        recompute={recomputeRisk}
        onDecisionsChange={setRiskDecisions}
        onCancelAndComplete={() => {
          // La cancelación GLOBAL de la sesión la maneja el modal padre
          // (botón "Cancelar" ya existente) — acá no hay una acción propia.
        }}
      />

      {impact && (
        <div>
          <p className="mb-1 text-xs font-semibold text-vk-text-primary">
            Impacto proyectado en productos
          </p>
          <ImportImpactSummary impact={impact} />
        </div>
      )}

      {activeSheet && (
        <div>
          <p className="mb-1 text-xs font-semibold text-vk-text-primary">
            Filas de ejemplo — {activeSheet.label}
          </p>
          <DataSample
            rows={
              mappingContexts.find((c) => c.context_id === activeContextId)?.preview_rows ?? []
            }
            columns={
              mappingContexts.find((c) => c.context_id === activeContextId)?.headers ?? undefined
            }
          />
        </div>
      )}

      {hasChanges && (
        <button
          type="button"
          onClick={handleUpdatePreview}
          disabled={updating}
          className="flex items-center gap-1.5 rounded-lg border border-vk-blue px-3 py-1.5 text-xs font-medium text-vk-blue hover:bg-vk-blue/10 transition-colors disabled:opacity-50"
        >
          {updating ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
          Actualizar vista previa con la corrección
        </button>
      )}
    </div>
  );
}
