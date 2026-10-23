import { api } from "@/lib/api";
import type { LegacyFiscalCondition } from "@/lib/fiscalCondition";

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
  /** Fondo de caja del último cierre con arqueo (sugerencia). */
  suggested_opening_float_ars: number | null;
  /**
   * Régimen fiscal — solo adapta la guía del modal. Tolera el valor legacy
   * "registered" mientras el backend migra a monotributo/responsable_inscripto.
   */
  fiscal_condition: LegacyFiscalCondition | null;
}

export interface CreateCashClosePayload {
  close_date: string;
  counted_total_ars: number;
  counted_by_method?: Record<string, number>;
  notes?: string | null;
  // Arqueo estructurado (opcionales; el flujo simple sigue andando).
  opening_float_ars?: number;
  cash_denominations?: Record<string, number>;
  voucher_expenses_ars?: number;
  register_surplus_as_income?: boolean;
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
  opening_float_ars: number | null;
  cash_denominations: Record<string, number> | null;
  voucher_expenses_ars: number | null;
  result_code: "BALANCED" | "SURPLUS" | "SHORTAGE" | null;
  surplus_registered: boolean;
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
