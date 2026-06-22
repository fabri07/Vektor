import { api } from "@/lib/api";

export interface CreateSupplierPayload {
  name: string;
  last_name?: string | null;
  cuil?: string | null;
  payment_method?: string | null;
  email?: string | null;
  phone?: string | null;
  notes?: string | null;
  custom_fields?: Record<string, unknown>;
}

export type UpdateSupplierPayload = Partial<CreateSupplierPayload>;

export interface SupplierResponse {
  id: string;
  tenant_id: string;
  name: string;
  last_name: string | null;
  cuil: string | null;
  payment_method: string | null;
  email: string | null;
  phone: string | null;
  notes: string | null;
  custom_fields?: Record<string, unknown>;
  is_active: boolean;
  created_at: string;
}

export interface SupplierProductPurchase {
  product_id: string;
  name: string;
  last_purchase_at: string | null;
  total_qty: number;
  unit_price: number;
}

export interface ReceiptLinePayload {
  product_name: string;
  sku?: string;
  qty: number;
  unit_price: number;
}

export interface UploadReceiptPayload {
  lines: ReceiptLinePayload[];
  shipping_cost?: number;
  currency: "ARS";
  transaction_date?: string;
}

export interface SuppliersListParams {
  is_active?: boolean;
  limit?: number;
  offset?: number;
}

const PAGE_SIZE = 200;
const MAX_PAGES = 25;

export const suppliersService = {
  async createSupplier(
    payload: CreateSupplierPayload,
    idempotencyKey?: string,
  ): Promise<SupplierResponse> {
    const res = await api.post<SupplierResponse>(
      "/suppliers",
      payload,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    );
    return res.data;
  },

  async updateSupplier(id: string, payload: UpdateSupplierPayload): Promise<SupplierResponse> {
    const res = await api.patch<SupplierResponse>(`/suppliers/${id}`, payload);
    return res.data;
  },

  async deleteSupplier(id: string): Promise<{ message: string }> {
    const res = await api.delete<{ message: string }>(`/suppliers/${id}`);
    return res.data;
  },

  async getSupplier(id: string): Promise<SupplierResponse> {
    const res = await api.get<SupplierResponse>(`/suppliers/${id}`);
    return res.data;
  },

  async getSuppliers(params?: SuppliersListParams): Promise<SupplierResponse[]> {
    const res = await api.get<SupplierResponse[]>("/suppliers", { params });
    return res.data;
  },

  async getAllSuppliers(
    params?: Omit<SuppliersListParams, "limit" | "offset">,
  ): Promise<SupplierResponse[]> {
    const items: SupplierResponse[] = [];

    for (let page = 0; page < MAX_PAGES; page += 1) {
      const batch = await suppliersService.getSuppliers({
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

  async getSupplierProducts(id: string): Promise<SupplierProductPurchase[]> {
    const res = await api.get<SupplierProductPurchase[]>(`/suppliers/${id}/products`);
    return res.data;
  },

  async uploadReceipt(
    supplierId: string,
    payload: UploadReceiptPayload,
    idempotencyKey?: string,
  ): Promise<unknown> {
    const res = await api.post<unknown>(
      `/suppliers/${supplierId}/receipts`,
      payload,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    );
    return res.data;
  },
};
