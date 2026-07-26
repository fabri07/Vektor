import { api } from "@/lib/api";
import type { AxiosError } from "axios";

// El preview estima en memoria (sub-segundo) y el undo es acotado; igual se da
// margen sobre el timeout global de 15s del cliente. El APPLY ya NO va por acá:
// corre en background (Celery) y el frontend hace polling del estado.
const REREAD_TIMEOUT_MS = 120_000;

// F4: el confirm corre el import SÍNCRONO (inline) y puede tardar minutos en
// archivos grandes sobre Neon. El timeout global de 15s cortaría al usuario con
// un error aunque el backend siga importando (y un reintento daría 409). Se le da
// margen POR ENCIMA del TTL del lease del backend (15 min) para que un import
// legítimamente largo termine; si aun así vence, el caller lo trata como
// "sigue en curso" (no como error) y refresca el estado desde la lista.
const CONFIRM_TIMEOUT_MS = 16 * 60_000; // 16 min > TTL del lease (15 min)

export interface UploadedFileItem {
  id: string;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  purpose: string;
  processing_status: string;
  created_at: string;
}

export interface ColumnAtRisk {
  column: string;
  null_pct: number;
  recommendation: string;
}

// F8c: diagnóstico de riesgo por columna dentro de un contexto (reemplaza el
// legacy ColumnAtRisk global). null_ratio es 0.0–1.0 → la UI lo muestra ×100.
export interface ContextualColumnRisk {
  context_id: string;
  entity_type: string;
  source_column: string;
  target_field: string;
  null_ratio: number; // 0.0–1.0
  affected_rows: number;
  null_rows: number;
  invalid_rows: number;
  field_requirement: "required" | "explicitly_selected" | "optional";
  mapping_source: "tenant_history" | "heuristic" | "fuzzy" | "llm" | "none";
  user_selected: boolean;
  allowed_actions: string[]; // "drop_column" | "route_affected_rows_to_others"
  recommendation: string;
}

// F8c: decisión del usuario sobre una columna riesgosa dentro de un contexto.
export interface ColumnRiskDecision {
  context_id: string;
  source_column: string;
  target_field: string;
  action: "drop_column" | "route_affected_rows_to_others";
}

// F7d: fila de muestra del preview de un maestro (cliente/proveedor). El
// backend minimiza PII a propósito — nunca trae DNI/CUIT/email/teléfono
// crudos, solo nombre + estado + un diagnóstico corto.
export interface MasterPreviewSample {
  row_index: number;
  status: "create" | "update" | "invalid" | "duplicate_in_file" | "needs_review";
  display_name: string | null;
  existing_name: string | null;
  issue: string | null;
}

// F7d: preview de una hoja de maestro — cuántas filas son create/update/
// needs_review/invalid/duplicate ANTES de confirmar. Solo diagnóstico, no
// persiste nada; el confirm real solo importa create/update.
export interface MasterPreviewSummary {
  context_id: string | null;
  entity_type: "customer" | "supplier";
  to_create: number;
  to_update: number;
  needs_review: number;
  invalid: number;
  duplicates: number;
  samples: MasterPreviewSample[];
}

export interface FilePreview {
  file_id: string;
  processing_status: string;
  parsed_summary_json: Record<string, unknown> | null;
  columns_at_risk: ColumnAtRisk[];
  // F8c: diagnóstico contextual por columna; opcional para archivos viejos que
  // no lo hayan traído en su parsed_summary_json.
  contextual_column_risk?: ContextualColumnRisk[];
  // F7e: vacío si el archivo no tiene hojas de maestro o no se pudo estimar el mapeo.
  master_previews: MasterPreviewSummary[];
}

export interface ConfirmIngestionResult {
  file_id: string;
  status: string;
  message: string;
  // Avisos human-in-the-loop tras confirmar (compras sin proveedor/producto, filas a
  // "Otros"). No bloquean; se muestran en un banner para que el usuario los revise.
  warnings?: string[];
}

/**
 * Tratamiento del stock de un archivo de catálogo/lista al confirmar:
 * - "opening_balance": saldo de apertura (ya lo tenía) → solo carga inventario,
 *   sin gasto de mercadería ni salida de caja.
 * - "purchase": compra → registra el gasto (COGS) y la baja de caja.
 */
export type StockTreatment = "opening_balance" | "purchase";

// Respuesta del upload: incluye un aviso (warning) opcional — p. ej. re-subida
// por nombre (versión actualizada) — y el id del archivo original si es duplicado.
export interface UploadResult {
  file_id: string;
  status: string;
  duplicate_of?: string | null;
  warning?: string | null;
}

export interface ColumnMappingSuggestion {
  source_column: string;
  normalized_column: string;
  sample_values: string[];
  target_field: string | null;
  confidence: number;
  // FASE 2 (A2): "llm" = la 4ª capa LLM desambiguó esta columna.
  source: "tenant_history" | "heuristic" | "fuzzy" | "llm" | "none";
  status: "mapped" | "unmapped" | "required_missing";
  context_id?: string | null;
}

export interface ColumnMapping {
  source_column: string;
  target_field: string; // campo canónico, "ignore", o "custom_field:{key}"
  context_id?: string | null;
  entity_type?: string | null;
  // F8c: el frontend lo marca en true solo cuando el usuario cambió el target
  // manualmente. El backend nunca lo infiere — es distinto de `source`/
  // `mapping_source` (que indican de dónde salió la sugerencia).
  user_selected?: boolean;
}

/** Contexto de mapeo: una hoja/tabla/grupo detectado dentro de un archivo. */
export interface MappingContext {
  context_id: string;
  label: string;
  source_kind: "sheet" | "table" | "text_group" | "ocr_group";
  entity_type: "sale" | "expense" | "product" | "customer" | "supplier" | null;
  headers: string[] | null;
  fields: string[] | null;
  preview_rows: Record<string, unknown>[];
  row_count: number;
}

export interface TenantColumnMapping {
  id: string;
  entity_type: string;
  source_column: string;
  target_field: string;
  confirmed_count: number;
  last_seen_at: string;
}

// ── Relectura de archivos (REREAD_FILE) ──────────────────────────────────────
// Espejo de los schemas backend en app/schemas/ingestion.py.

export interface RereadCounts {
  to_update: number;
  preserved: number;
  new: number;
  to_void: number;
  unchanged: number;
  products_new: number;
  products_restock: number;
}

export interface RereadItem {
  action: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

export interface RereadPreviewResponse {
  file_id: string;
  counts: RereadCounts;
  legacy_fallback: boolean;
  sample_changes: RereadItem[];
}

export interface RereadApplyResponse {
  file_id: string;
  run_id: string;
  to_update: number;
  preserved: number;
  new: number;
  voided: number;
  inserted: number;
  legacy_fallback: boolean;
  items: RereadItem[];
}

// El apply corre en background; el POST devuelve el run para hacer polling.
export interface RereadApplyStartResponse {
  file_id: string;
  run_id: string;
  status: string; // "RUNNING"
}

export interface RereadRunStatusResponse {
  run_id: string;
  file_id: string;
  status: string; // RUNNING | APPLIED | FAILED
  to_update: number;
  preserved: number;
  new: number;
  voided: number;
  inserted: number;
  legacy_fallback: boolean;
  items: RereadItem[];
  error: string | null;
}

export interface RereadUndoResponse {
  run_id: string;
  restored: number;
  removed: number;
  status: string;
}

export const ingestionService = {
  async upload(
    file: File,
    fileHint: string = "general",
    onProgress?: (percent: number) => void,
    // Override explícito: reimportar un archivo cuyo contenido EXACTO ya fue
    // importado (el backend, por defecto, lo bloquea con 409 para no duplicar).
    allowDuplicate: boolean = false,
  ): Promise<UploadResult> {
    const fd = new FormData();
    fd.append("file", file);
    const qs = new URLSearchParams({ file_hint: fileHint });
    if (allowDuplicate) qs.set("allow_duplicate", "true");
    const res = await api.post<UploadResult>(
      `/ingestion/upload?${qs.toString()}`,
      fd,
      {
        headers: { "Content-Type": "multipart/form-data" },
        onUploadProgress: onProgress
          ? (e) => {
              const total = e.total ?? 1;
              onProgress(Math.round((e.loaded / total) * 100));
            }
          : undefined,
      },
    );
    return res.data;
  },

  async listFiles(): Promise<UploadedFileItem[]> {
    const res = await api.get<UploadedFileItem[]>("/ingestion/files");
    return res.data;
  },

  /**
   * Returns null when the file is still PENDING/PROCESSING (backend returns 409).
   * Throws on other errors (404, 500, etc.).
   */
  async getPreview(fileId: string): Promise<FilePreview | null> {
    try {
      const res = await api.get<FilePreview>(
        `/ingestion/files/${fileId}/preview`,
      );
      return res.data;
    } catch (err) {
      const axiosErr = err as AxiosError;
      if (axiosErr.response?.status === 409) return null;
      throw err;
    }
  },

  async getColumnMappings(
    fileId: string,
    entityType: string = "sale",
    contextId?: string,
  ): Promise<ColumnMappingSuggestion[]> {
    const params = new URLSearchParams({ entity_type: entityType });
    if (contextId) params.set("context_id", contextId);
    const res = await api.get<ColumnMappingSuggestion[]>(
      `/ingestion/files/${fileId}/column-mappings?${params.toString()}`,
    );
    return res.data;
  },

  async getLearnedMappings(): Promise<TenantColumnMapping[]> {
    const res = await api.get<TenantColumnMapping[]>("/ingestion/column-mappings");
    return res.data;
  },

  async deleteLearnedMapping(mappingId: string): Promise<void> {
    await api.delete(`/ingestion/column-mappings/${mappingId}`);
  },

  async confirmFile(
    fileId: string,
    confirmedFields: Record<string, boolean>,
    columnMappings?: ColumnMapping[],
    contextConfirmed?: Record<string, boolean>,
    contextEntity?: Record<string, string>,
    // Solo relevante cuando el archivo trae stock/productos: cómo tratar ese stock.
    stockTreatment?: StockTreatment,
    // F8c: decisiones del usuario sobre columnas riesgosas (drop / enrutar a Otros).
    columnRiskDecisions?: ColumnRiskDecision[],
  ): Promise<ConfirmIngestionResult> {
    const res = await api.post<ConfirmIngestionResult>(
      `/ingestion/files/${fileId}/confirm`,
      {
        confirmed_fields: confirmedFields,
        column_mappings: columnMappings ?? [],
        context_confirmed: contextConfirmed ?? {},
        context_entity: contextEntity ?? {},
        stock_treatment: stockTreatment ?? null,
        column_risk_decisions: columnRiskDecisions ?? [],
      },
      { timeout: CONFIRM_TIMEOUT_MS },
    );
    return res.data;
  },

  // F8c: recalcula el riesgo contextual de columnas en vivo (p. ej. tras
  // cambiar un mapeo o confirmar un contexto), sin persistir nada.
  async recomputeColumnRisk(
    fileId: string,
    body: {
      columnMappings: ColumnMapping[];
      contextEntity: Record<string, string>;
      confirmedFields: Record<string, boolean>;
      contextConfirmed: Record<string, boolean>;
    },
    signal?: AbortSignal,
  ): Promise<ContextualColumnRisk[]> {
    const res = await api.post<ContextualColumnRisk[]>(
      `/ingestion/files/${fileId}/column-risk`,
      {
        column_mappings: body.columnMappings,
        context_entity: body.contextEntity,
        confirmed_fields: body.confirmedFields,
        context_confirmed: body.contextConfirmed,
      },
      { signal },
    );
    return res.data;
  },

  async cancelFile(fileId: string): Promise<{ file_id: string; status: string }> {
    const res = await api.post<{ file_id: string; status: string }>(
      `/ingestion/files/${fileId}/cancel`,
    );
    return res.data;
  },

  async deleteFile(fileId: string): Promise<void> {
    await api.delete(`/ingestion/files/${fileId}`);
  },

  async reprocessFile(fileId: string): Promise<void> {
    await api.post(`/ingestion/files/${fileId}/reprocess`);
  },

  // ── Relectura de archivos (REREAD_FILE) ──────────────────────────────────
  async rereadPreview(fileId: string): Promise<RereadPreviewResponse> {
    const res = await api.post<RereadPreviewResponse>(
      `/ingestion/files/${fileId}/reread/preview`,
      undefined,
      // Relectura sobre archivos grandes (miles de filas) puede tardar más que
      // el default de 15s del cliente; el edge de Railway corta a ~300s.
      { timeout: REREAD_TIMEOUT_MS },
    );
    return res.data;
  },

  // Encola el apply en background y devuelve el run para hacer polling.
  async rereadApply(fileId: string): Promise<RereadApplyStartResponse> {
    const res = await api.post<RereadApplyStartResponse>(
      `/ingestion/files/${fileId}/reread/apply`,
    );
    return res.data;
  },

  async rereadRunStatus(
    fileId: string,
    runId: string,
  ): Promise<RereadRunStatusResponse> {
    const res = await api.get<RereadRunStatusResponse>(
      `/ingestion/files/${fileId}/reread/runs/${runId}`,
    );
    return res.data;
  },

  async rereadUndo(fileId: string): Promise<RereadUndoResponse> {
    const res = await api.post<RereadUndoResponse>(
      `/ingestion/files/${fileId}/reread/undo`,
      undefined,
      { timeout: REREAD_TIMEOUT_MS },
    );
    return res.data;
  },
};
