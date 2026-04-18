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

export interface FilePreview {
  file_id: string;
  processing_status: string;
  parsed_summary_json: Record<string, unknown> | null;
}

export interface ConfirmIngestionResult {
  file_id: string;
  status: string;
  message: string;
}

export const ingestionService = {
  async upload(
    file: File,
    fileHint: string = "general",
  ): Promise<{ file_id: string; status: string }> {
    const fd = new FormData();
    fd.append("file", file);
    const res = await api.post<{ file_id: string; status: string }>(
      `/ingestion/upload?file_hint=${fileHint}`,
      fd,
      { headers: { "Content-Type": "multipart/form-data" } },
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

  async confirmFile(
    fileId: string,
    confirmedFields: Record<string, boolean>,
  ): Promise<ConfirmIngestionResult> {
    const res = await api.post<ConfirmIngestionResult>(
      `/ingestion/files/${fileId}/confirm`,
      { confirmed_fields: confirmedFields },
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
