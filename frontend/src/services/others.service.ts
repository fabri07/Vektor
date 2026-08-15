import { api } from "@/lib/api";

/**
 * F2-T2b: candidato de producto existente para vincular una fila de "Otros"
 * ambigua/en conflicto en vez de crear un duplicado.
 */
export interface ProductMatchCandidate {
  id: string;
  /** Qué campos matchearon (p. ej. ["sku"], ["barcode"], ["name", "sku"]). */
  matched_by: string[];
  name: string | null;
  sku: string | null;
  barcode: string | null;
}

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
  /**
   * F2-T2b: candidatos de producto existente para VINCULAR (solo aplica a filas
   * de producto ambiguas/en conflicto). null fuera de ese caso.
   */
  match_candidates: ProductMatchCandidate[] | null;
  status: "PENDING" | "IMPORTED" | "DISMISSED";
  created_at: string;
}

export type ReclassifyEntityType = "sale" | "expense" | "product" | "customer" | "supplier";

export interface ReclassifyPayload {
  entity_type: ReclassifyEntityType;
  fields: Record<string, unknown>;
  /**
   * F2-T2b: solo para entity_type="product". Si viene, se VINCULA el registro a
   * ese producto existente (uno de los `match_candidates`) en vez de crear uno
   * nuevo. El backend re-valida que el id pertenezca al tenant y esté activo.
   */
  target_product_id?: string;
}

export interface ResolvePurchasePayload {
  target_product_id: string;
  amount: number;
  quantity: number;
  unit_cost?: number;
  transaction_date: string;
  payment_method: string;
  category?: string;
  description?: string;
  supplier_name?: string;
}

export interface BulkImportResult {
  imported_sales: number;
  imported_expenses: number;
  skipped: number; // ya importados (idempotencia)
  needs_manual: number; // no parsearon (fecha/monto ilegible) → completar a mano
}

/** F-O.3: un grupo de "Otros" — mismo archivo × procedencia × motivo × sugerencia × estado. */
export interface UnclassifiedGroupSummary {
  uploaded_file_id: string | null;
  original_filename: string | null;
  source: "ingestion" | "chat" | "reanalysis";
  context_label: string | null;
  suggested_entity: "sale" | "expense" | "product" | null;
  status: "PENDING" | "IMPORTED" | "DISMISSED";
  count: number;
}

export interface OthersListFilters {
  uploaded_file_id?: string | null;
  context_label?: string | null;
  suggested_entity?: string | null;
  source?: string | null;
}

export interface DismissGroupPayload {
  uploaded_file_id: string | null;
  source: string;
  context_label: string | null;
  suggested_entity: string | null;
  status: string;
  expected_count: number;
}

export const othersService = {
  async getPending(
    offset = 0,
    limit = 50,
    filters: OthersListFilters = {},
  ): Promise<UnclassifiedRecordResponse[]> {
    const res = await api.get<UnclassifiedRecordResponse[]>("/others", {
      params: {
        status: "PENDING",
        limit,
        offset,
        uploaded_file_id: filters.uploaded_file_id ?? undefined,
        context_label: filters.context_label ?? undefined,
        suggested_entity: filters.suggested_entity ?? undefined,
        source: filters.source ?? undefined,
      },
    });
    return res.data;
  },

  /** F-O.3: agrupado por archivo × procedencia × motivo × sugerencia. */
  async getSummary(): Promise<UnclassifiedGroupSummary[]> {
    const res = await api.get<UnclassifiedGroupSummary[]>("/others/summary");
    return res.data;
  },

  /**
   * Descarta un grupo entero de una vez. `expected_count` es el conteo que el
   * usuario vio en `getSummary()` — si cambió (relectura entre medio), el
   * backend responde 409 en vez de descartar de más o de menos.
   */
  async dismissGroup(payload: DismissGroupPayload): Promise<number> {
    const res = await api.post<{ dismissed: number }>("/others/dismiss-group", payload);
    return res.data.dismissed;
  },

  async getPendingCount(): Promise<number> {
    const res = await api.get<{ pending: number }>("/others/count");
    return res.data.pending;
  },

  async reclassify(id: string, payload: ReclassifyPayload): Promise<void> {
    await api.post(`/others/${id}/reclassify`, payload);
  },

  async resolvePurchase(id: string, payload: ResolvePurchasePayload): Promise<void> {
    await api.post(`/others/${id}/resolve-purchase`, payload);
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
