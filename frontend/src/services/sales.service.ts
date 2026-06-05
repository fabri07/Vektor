import { api } from "@/lib/api";
import type { SaleSummaryResponse } from "@/types/api";

export type { SaleSummaryResponse };

export interface CreateSalePayload {
  amount: number;
  quantity: number;
  transaction_date: string; // YYYY-MM-DD
  payment_method: string;
  product_id?: string | null;
  notes?: string | null;
}

export type UpdateSalePayload = Partial<CreateSalePayload>;

export interface SaleEntryResponse {
  id: string;
  tenant_id: string;
  product_id: string | null;
  amount: number;
  quantity: number;
  transaction_date: string;
  payment_method: string;
  notes: string | null;
  custom_fields?: Record<string, unknown>;
  created_at: string;
}

export interface SalesListParams {
  from_date?: string;
  to_date?: string;
  limit?: number;
  offset?: number;
}

export interface DateRangeResponse {
  min_date: string | null;
  max_date: string | null;
}

const PAGE_SIZE = 200;
const MAX_PAGES = 25;

export const salesService = {
  async createSale(payload: CreateSalePayload): Promise<SaleEntryResponse> {
    const res = await api.post<SaleEntryResponse>("/sales", payload);
    return res.data;
  },

  async updateSale(id: string, payload: UpdateSalePayload): Promise<SaleEntryResponse> {
    const res = await api.patch<SaleEntryResponse>(`/sales/${id}`, payload);
    return res.data;
  },

  async deleteSale(id: string): Promise<void> {
    await api.delete(`/sales/${id}`);
  },

  async getSummary(): Promise<SaleSummaryResponse> {
    const res = await api.get<SaleSummaryResponse>("/sales/summary");
    return res.data;
  },

  async getDateRange(): Promise<DateRangeResponse> {
    const res = await api.get<DateRangeResponse>("/sales/date-range");
    return res.data;
  },

  async getEntries(params?: SalesListParams): Promise<SaleEntryResponse[]> {
    const res = await api.get<SaleEntryResponse[]>("/sales", { params });
    return res.data;
  },

  async getAllEntries(params?: Omit<SalesListParams, "limit" | "offset">): Promise<SaleEntryResponse[]> {
    const items: SaleEntryResponse[] = [];

    for (let page = 0; page < MAX_PAGES; page += 1) {
      const batch = await salesService.getEntries({
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
};
