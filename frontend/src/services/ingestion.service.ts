import { api } from "@/lib/api";
import type { AxiosError } from "axios";

// La relectura reprocesa el archivo completo; necesita más margen que el
// timeout global de 15s del cliente axios. El preview ahora estima en memoria
// (sub-segundo), pero el APPLY inserta todas las filas de verdad: en archivos
// grandes puede tardar varios minutos, así que usa un timeout más alto (cerca del
// límite del edge de Railway, ~300s). Si aun así no alcanza, el apply debería
// pasar a procesamiento en background.
const REREAD_TIMEOUT_MS = 120_000;
const REREAD_APPLY_TIMEOUT_MS = 290_000;

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

export interface FilePreview {
  file_id: string;
  processing_status: string;
  parsed_summary_json: Record<string, unknown> | null;
  columns_at_risk: ColumnAtRisk[];
}

export interface ConfirmIngestionResult {
  file_id: string;
  status: string;
  message: string;
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
}

/** Contexto de mapeo: una hoja/tabla/grupo detectado dentro de un archivo. */
export interface MappingContext {
  context_id: string;
  label: string;
  source_kind: "sheet" | "table" | "text_group" | "ocr_group";
  entity_type: "sale" | "expense" | "product" | null;
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
  ): Promise<{ file_id: string; status: string }> {
    const fd = new FormData();
    fd.append("file", file);
    const res = await api.post<{ file_id: string; status: string }>(
      `/ingestion/upload?file_hint=${fileHint}`,
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
  ): Promise<ConfirmIngestionResult> {
    const res = await api.post<ConfirmIngestionResult>(
      `/ingestion/files/${fileId}/confirm`,
      {
        confirmed_fields: confirmedFields,
        column_mappings: columnMappings ?? [],
        context_confirmed: contextConfirmed ?? {},
        context_entity: contextEntity ?? {},
      },
    );
    return res.data;
  },

  async dropColumns(
    fileId: string,
    columns: string[],
  ): Promise<{ file_id: string; dropped_columns: string[] }> {
    const res = await api.post<{ file_id: string; dropped_columns: string[] }>(
      `/ingestion/files/${fileId}/drop-columns`,
      { columns },
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

  async rereadApply(fileId: string): Promise<RereadApplyResponse> {
    const res = await api.post<RereadApplyResponse>(
      `/ingestion/files/${fileId}/reread/apply`,
      undefined,
      // El apply inserta todo el archivo: margen más alto que el preview.
      { timeout: REREAD_APPLY_TIMEOUT_MS },
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
