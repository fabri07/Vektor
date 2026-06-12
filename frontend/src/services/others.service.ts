import { api } from "@/lib/api";

/** Registro sin clasificar en la bandeja "Otros" (unclassified_records). */
export interface UnclassifiedRecordResponse {
  id: string;
  uploaded_file_id: string | null;
  source: "ingestion" | "chat" | "reanalysis";
  context_label: string | null;
  headers: string[] | null;
  row_data: Record<string, string>;
  suggested_entity: "sale" | "expense" | "product" | null;
  /** Código canónico recomendado (catálogo de gastos o de productos según destino). */
  suggested_category: string | null;
  suggested_category_label: string | null;
  status: "PENDING" | "IMPORTED" | "DISMISSED";
  created_at: string;
}

export type ReclassifyEntityType = "sale" | "expense" | "product";

export interface ReclassifyPayload {
  entity_type: ReclassifyEntityType;
  fields: Record<string, unknown>;
}

export interface BulkImportResult {
  imported_sales: number;
  imported_expenses: number;
  skipped: number;
}

export const othersService = {
  async getPending(offset = 0, limit = 50): Promise<UnclassifiedRecordResponse[]> {
    const res = await api.get<UnclassifiedRecordResponse[]>("/others", {
      params: { status: "PENDING", limit, offset },
    });
    return res.data;
  },

  async getPendingCount(): Promise<number> {
    const res = await api.get<{ pending: number }>("/others/count");
    return res.data.pending;
  },

  async reclassify(id: string, payload: ReclassifyPayload): Promise<void> {
    await api.post(`/others/${id}/reclassify`, payload);
  },

  async dismiss(id: string): Promise<void> {
    await api.post(`/others/${id}/dismiss`);
  },

  /** Importa en lote todos los pendientes sugeridos como venta/gasto. */
  async bulkImport(entityType?: "sale" | "expense"): Promise<BulkImportResult> {
    const res = await api.post<BulkImportResult>("/others/bulk-import", {
      entity_type: entityType ?? null,
    });
    return res.data;
  },
};
