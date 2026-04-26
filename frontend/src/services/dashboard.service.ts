import { api } from "@/lib/api";
import type {
  LatestScoreResponse,
  CurrentInsightResponse,
  BusinessBreakdownResponse,
  CashForecastResponse,
} from "@/types/api";

export async function fetchLatestScore(): Promise<LatestScoreResponse> {
  const { data } = await api.get<LatestScoreResponse>("/health-scores/latest");
  return data;
}

export async function fetchCurrentInsight(): Promise<CurrentInsightResponse> {
  const { data } = await api.get<CurrentInsightResponse>("/insights/current");
  return data;
}

export async function acknowledgeAction(id: string): Promise<void> {
  await api.patch(`/insights/actions/${id}/acknowledge`);
}

export async function fetchBusinessBreakdown(days = 30): Promise<BusinessBreakdownResponse> {
  const { data } = await api.get<BusinessBreakdownResponse>(`/insights/breakdown?days=${days}`);
  return data;
}

export async function fetchCashForecast(refresh = false): Promise<CashForecastResponse> {
  const { data } = await api.get<CashForecastResponse>(
    `/forecast/cash${refresh ? "?refresh=true" : ""}`,
  );
  return data;
}
