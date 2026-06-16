import { api } from "@/lib/api";

/** Plataformas soportadas para métricas de redes sociales. */
export type SocialPlatform = "instagram" | "facebook" | "tiktok" | "other";

/** Métrica de redes sociales cargada manualmente o por CSV. */
export interface SocialMetricResponse {
  id: string;
  tenant_id: string;
  platform: SocialPlatform;
  metric_date: string;
  followers: number;
  reach: number;
  engagement: number;
  ads_spend_ars: number;
  created_at: string;
}

/** Payload para crear una métrica. Los contadores son opcionales (default 0 en backend). */
export interface CreateSocialMetricPayload {
  platform: string;
  metric_date: string;
  followers?: number;
  reach?: number;
  engagement?: number;
  ads_spend_ars?: number;
}

/** Payload para actualizar una métrica existente (todos los campos opcionales). */
export type UpdateSocialMetricPayload = Partial<CreateSocialMetricPayload>;

/** Resumen por plataforma en el dashboard (followers = último valor; resto = suma del período). */
export interface MarketingPlatformBreakdown {
  platform: string;
  followers: number;
  reach: number;
  engagement: number;
  ads_spend_ars: number;
}

/** Comparación de gasto en ads vs revenue del período. */
export interface MarketingAdsVsSales {
  ads_spend_ars: number;
  revenue_ars: number;
  ratio: number | null;
}

/** Dashboard agregado de marketing (shape del backend: totales planos). */
export interface MarketingDashboard {
  days: number;
  from_date: string;
  to_date: string;
  has_data: boolean;
  platforms: MarketingPlatformBreakdown[];
  total_followers: number;
  total_reach: number;
  total_engagement: number;
  total_ads_spend_ars: number;
  ads_vs_sales: MarketingAdsVsSales;
}

export interface GetMetricsParams {
  platform?: string;
  from_date?: string;
  to_date?: string;
}

export const marketingService = {
  async getMetrics(params?: GetMetricsParams): Promise<SocialMetricResponse[]> {
    const res = await api.get<SocialMetricResponse[]>("/marketing/metrics", {
      params: {
        platform: params?.platform || undefined,
        from_date: params?.from_date || undefined,
        to_date: params?.to_date || undefined,
      },
    });
    return res.data;
  },

  async createMetric(payload: CreateSocialMetricPayload): Promise<SocialMetricResponse> {
    const res = await api.post<SocialMetricResponse>("/marketing/metrics", payload);
    return res.data;
  },

  /** Carga en lote. El backend espera un array plano de métricas. */
  async bulkCreate(items: CreateSocialMetricPayload[]): Promise<SocialMetricResponse[]> {
    const res = await api.post<SocialMetricResponse[]>("/marketing/metrics/bulk", items);
    return res.data;
  },

  async updateMetric(
    id: string,
    payload: UpdateSocialMetricPayload,
  ): Promise<SocialMetricResponse> {
    const res = await api.patch<SocialMetricResponse>(`/marketing/metrics/${id}`, payload);
    return res.data;
  },

  async deleteMetric(id: string): Promise<{ message: string }> {
    const res = await api.delete<{ message: string }>(`/marketing/metrics/${id}`);
    return res.data;
  },

  async getDashboard(days = 30): Promise<MarketingDashboard> {
    const res = await api.get<MarketingDashboard>("/marketing/dashboard", {
      params: { days },
    });
    return res.data;
  },
};
