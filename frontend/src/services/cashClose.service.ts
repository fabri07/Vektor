import { api } from "@/lib/api";

export interface CashMethodBreakdown {
  payment_method: string;
  expected_ars: number;
}

export interface CashClosePreview {
  close_date: string;
  expected_total_ars: number;
  breakdown: CashMethodBreakdown[];
  already_closed: boolean;
  is_past_close_now: boolean;
}

export interface CreateCashClosePayload {
  close_date: string;
  counted_total_ars: number;
  counted_by_method?: Record<string, number>;
  notes?: string | null;
}

export interface CashCloseResponse {
  id: string;
  close_date: string;
  expected_total_ars: number;
  counted_total_ars: number;
  difference_ars: number;
  breakdown_by_method: Record<string, { expected: number; counted: number | null }>;
  notes: string | null;
  closed_by_user_id: string | null;
  created_at: string;
}

export const cashCloseService = {
  async getPreview(closeDate: string): Promise<CashClosePreview> {
    const res = await api.get<CashClosePreview>("/cash-closes/preview", {
      params: { close_date: closeDate },
    });
    return res.data;
  },

  async createClose(payload: CreateCashClosePayload): Promise<CashCloseResponse> {
    const res = await api.post<CashCloseResponse>("/cash-closes", payload);
    return res.data;
  },

  async listCloses(fromDate: string, toDate: string): Promise<CashCloseResponse[]> {
    const res = await api.get<CashCloseResponse[]>("/cash-closes", {
      params: { from_date: fromDate, to_date: toDate },
    });
    return res.data;
  },
};
