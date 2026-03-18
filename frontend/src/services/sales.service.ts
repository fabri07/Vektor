import { api } from "@/lib/api";

export interface CreateSalePayload {
  amount: number;
  quantity: number;
  transaction_date: string; // YYYY-MM-DD
  payment_method: string;
  product_id?: string | null;
  notes?: string | null;
}

export interface SaleEntryResponse {
  id: string;
  tenant_id: string;
  product_id: string | null;
  amount: number;
  quantity: number;
  transaction_date: string;
  payment_method: string;
  notes: string | null;
  created_at: string;
}

export const salesService = {
  async createSale(payload: CreateSalePayload): Promise<SaleEntryResponse> {
    const res = await api.post<SaleEntryResponse>("/sales/", payload);
    return res.data;
  },
};
