import { api } from "@/lib/api";
import type { AxiosError } from "axios";

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
  source: "tenant_history" | "heuristic" | "fuzzy" | "none";
  status: "mapped" | "unmapped" | "required_missing";
}

export interface ColumnMapping {
  source_column: string;
  target_field: string; // campo canónico, "ignore", o "custom_field:{key}"
}

export interface TenantColumnMapping {
  id: string;
  entity_type: string;
  source_column: string;
  target_field: string;
  confirmed_count: number;
  last_seen_at: string;
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
  ): Promise<ColumnMappingSuggestion[]> {
    const res = await api.get<ColumnMappingSuggestion[]>(
      `/ingestion/files/${fileId}/column-mappings?entity_type=${entityType}`,
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
  ): Promise<ConfirmIngestionResult> {
    const res = await api.post<ConfirmIngestionResult>(
      `/ingestion/files/${fileId}/confirm`,
      {
        confirmed_fields: confirmedFields,
        column_mappings: columnMappings ?? [],
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
};
