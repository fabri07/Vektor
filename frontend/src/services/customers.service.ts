import { api } from "@/lib/api";

export interface CreateCustomerPayload {
  name: string;
  customer_type?: string | null;
  last_name?: string | null;
  doc_type?: string | null;
  dni?: string | null;
  cuit?: string | null;
  iva_condition?: string | null;
  email?: string | null;
  phone?: string | null;
  telegram_username?: string | null;
  address?: string | null;
  locality?: string | null;
  province?: string | null;
  postal_code?: string | null;
  birthday?: string | null;
  notes?: string | null;
  custom_fields?: Record<string, unknown>;
}

export type UpdateCustomerPayload = Partial<CreateCustomerPayload>;

export interface CustomerResponse {
  id: string;
  tenant_id: string;
  name: string;
  customer_type: string | null;
  last_name: string | null;
  doc_type: string | null;
  dni: string | null;
  cuit: string | null;
  iva_condition: string | null;
  email: string | null;
  phone: string | null;
  telegram_username: string | null;
  address: string | null;
  locality: string | null;
  province: string | null;
  postal_code: string | null;
  birthday: string | null;
  notes: string | null;
  custom_fields?: Record<string, unknown>;
  is_active: boolean;
  is_sentinel: boolean;
  created_at: string;
}

export interface CustomersListParams {
  is_active?: boolean;
  /** Incluir el cliente centinela "Local" (ventas sin cliente) si existe. */
  include_sentinel?: boolean;
  /** Incluir clientes dados de baja (se muestran en rojo). */
  include_inactive?: boolean;
  limit?: number;
  offset?: number;
}

// ── Carga por archivo (Fase B) ───────────────────────────────────────────────

export type ExtractionConfidence = "HIGH" | "MEDIUM" | "LOW";

/** Ficha individual sugerida al leer una foto/PDF/planilla. NO persiste nada. */
export interface CustomerExtractionResponse {
  customer_type: string | null;
  name: string | null;
  last_name: string | null;
  doc_type: string | null;
  dni: string | null;
  cuit: string | null;
  iva_condition: string | null;
  email: string | null;
  phone: string | null;
  address: string | null;
  locality: string | null;
  province: string | null;
  postal_code: string | null;
  birthday: string | null;
  confidence: ExtractionConfidence;
  warnings: string[];
  source_upload_id: string | null;
}

/** Una fila parseada del import masivo (payload del cliente, todo opcional). */
export interface CustomerImportRow {
  customer_type?: string | null;
  name?: string | null;
  last_name?: string | null;
  doc_type?: string | null;
  dni?: string | null;
  cuit?: string | null;
  iva_condition?: string | null;
  email?: string | null;
  phone?: string | null;
  address?: string | null;
  locality?: string | null;
  province?: string | null;
  postal_code?: string | null;
  birthday?: string | null;
}

export type ImportItemStatus = "create" | "update" | "invalid" | "duplicate_in_file";

export interface CustomerImportPreviewItem {
  row_index: number;
  status: ImportItemStatus;
  customer: CustomerImportRow;
  existing_id: string | null;
  existing_name: string | null;
  issues: string[];
}

export interface CustomerImportPreviewResponse {
  items: CustomerImportPreviewItem[];
  to_create: number;
  to_update: number;
  invalid: number;
  duplicates: number;
  warnings: string[];
  source_upload_id: string | null;
}

export interface CustomerImportConfirmResponse {
  created: number;
  updated: number;
  skipped: number;
  created_ids: string[];
  updated_ids: string[];
}

const PAGE_SIZE = 200;
const MAX_PAGES = 25;

export const customersService = {
  async createCustomer(
    payload: CreateCustomerPayload,
    idempotencyKey?: string,
  ): Promise<CustomerResponse> {
    const res = await api.post<CustomerResponse>(
      "/customers",
      payload,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    );
    return res.data;
  },

  async updateCustomer(id: string, payload: UpdateCustomerPayload): Promise<CustomerResponse> {
    const res = await api.patch<CustomerResponse>(`/customers/${id}`, payload);
    return res.data;
  },

  async deleteCustomer(id: string, force = false): Promise<{ message: string }> {
    const res = await api.delete<{ message: string }>(`/customers/${id}`, {
      params: force ? { force: true } : undefined,
    });
    return res.data;
  },

  async reactivateCustomer(id: string): Promise<CustomerResponse> {
    const res = await api.post<CustomerResponse>(`/customers/${id}/reactivate`);
    return res.data;
  },

  async getCustomer(id: string): Promise<CustomerResponse> {
    const res = await api.get<CustomerResponse>(`/customers/${id}`);
    return res.data;
  },

  async getCustomers(params?: CustomersListParams): Promise<CustomerResponse[]> {
    const res = await api.get<CustomerResponse[]>("/customers", { params });
    return res.data;
  },

  async getAllCustomers(
    params?: Omit<CustomersListParams, "limit" | "offset">,
  ): Promise<CustomerResponse[]> {
    const items: CustomerResponse[] = [];

    for (let page = 0; page < MAX_PAGES; page += 1) {
      const batch = await customersService.getCustomers({
        ...params,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      });
      items.push(...batch);

      if (batch.length < PAGE_SIZE) {
        break;
      }
    }

    return items;
  },

  /** Lee la ficha de UN cliente (foto/PDF/planilla) → sugerencia para prellenar. */
  async extractCustomer(file: File): Promise<CustomerExtractionResponse> {
    const formData = new FormData();
    formData.append("file", file);
    // NO setear Content-Type a mano: el browser/axios ponen el boundary del multipart.
    const res = await api.post<CustomerExtractionResponse>(
      "/customers/extract",
      formData,
    );
    return res.data;
  },

  /** Preview del import masivo: clasifica cada fila (no persiste). */
  async importPreview(file: File): Promise<CustomerImportPreviewResponse> {
    const formData = new FormData();
    formData.append("file", file);
    const res = await api.post<CustomerImportPreviewResponse>(
      "/customers/import/preview",
      formData,
    );
    return res.data;
  },

  /** Confirma el import: upsert idempotente de las filas elegidas. */
  async importConfirm(
    rows: CustomerImportRow[],
  ): Promise<CustomerImportConfirmResponse> {
    const res = await api.post<CustomerImportConfirmResponse>(
      "/customers/import/confirm",
      { rows },
    );
    return res.data;
  },
};
