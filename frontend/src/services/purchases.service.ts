import { api } from "@/lib/api";

export interface PurchaseLinePayload {
  product_id?: string | null;
  name?: string | null;
  category?: string | null;
  sku?: string | null;
  description?: string | null;
  unit_cost: number;
  quantity: number;
  sale_price_ars: number;
  update_price?: boolean;
}

export interface ManualPurchasePayload {
  supplier_id: string;
  payment_method: string;
  transaction_date: string;
  lines: PurchaseLinePayload[];
}

export interface PurchaseLineResult {
  product_id: string;
  product_name: string;
  created: boolean;
  expense_id: string;
  new_stock_units: number;
  margin_pct: number | null;
}

export interface ManualPurchaseResponse {
  lines: number;
  products_created: string[];
  expense_ids: string[];
  total_cogs: number;
  results: PurchaseLineResult[];
  meta: Record<string, unknown>;
}

export const purchasesService = {
  async createManual(
    payload: ManualPurchasePayload,
    idempotencyKey?: string,
  ): Promise<ManualPurchaseResponse> {
    const res = await api.post<ManualPurchaseResponse>(
      "/purchases/manual",
      payload,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    );
    return res.data;
  },
};
