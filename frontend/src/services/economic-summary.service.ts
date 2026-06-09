import { api } from "@/lib/api";

/** FASE 4: resumen económico analítico (NO contable) para un rango de fechas. */
export interface EconomicSummaryResponse {
  from_date: string; // YYYY-MM-DD
  to_date: string;
  total_income_ars: number;
  total_expenses_ars: number;
  net_result_ars: number;
  stock_value_ars: number;
  /** Productos activos sin costo: no suman al valor de stock, se reportan aparte. */
  missing_cost_count: number;
  missing_cost_stock_units: number;
  /** false → no hay movimientos ni stock cargado → empty state. */
  has_data: boolean;
}

export interface EconomicSummaryParams {
  from_date: string;
  to_date: string;
}

export const economicSummaryService = {
  async getSummary(params: EconomicSummaryParams): Promise<EconomicSummaryResponse> {
    const res = await api.get<EconomicSummaryResponse>("/economic-summary", { params });
    return res.data;
  },
};
