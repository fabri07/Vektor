import { api } from "@/lib/api";

/** Totales agregados de consumo de tokens y costo en el período. */
export interface UsageTotals {
  tokens_input: number;
  tokens_output: number;
  tokens_total: number;
  cost_usd: number;
  decisions: number;
}

/** Consumo por agente (desc por costo = top consumers). */
export interface UsageByAgent {
  agent: string;
  tokens_total: number;
  cost_usd: number;
}

/** Consumo por modelo. `priced=false` → el modelo no tiene precio configurado. */
export interface UsageByModel {
  model: string;
  tokens_input: number;
  tokens_output: number;
  cost_usd: number;
  priced: boolean;
}

/** Consumo por día (asc por fecha). */
export interface UsageByDay {
  date: string;
  tokens_total: number;
  cost_usd: number;
}

/** Consumo por tenant. */
export interface UsageByTenant {
  tenant_id: string;
  tokens_total: number;
  cost_usd: number;
}

/** Dashboard agregado de uso & costos (SUPERADMIN). */
export interface UsageDashboard {
  days: number;
  from_date: string;
  to_date: string;
  totals: UsageTotals;
  by_agent: UsageByAgent[];
  by_model: UsageByModel[];
  by_day: UsageByDay[];
  by_tenant: UsageByTenant[];
}

export const usageService = {
  async getUsage(days = 30, tenantId?: string): Promise<UsageDashboard> {
    const res = await api.get<UsageDashboard>("/admin/usage", {
      params: {
        days,
        tenant_id: tenantId || undefined,
      },
    });
    return res.data;
  },
};
