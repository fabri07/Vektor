import { api } from "@/lib/api";
import type {
  LatestScoreResponse,
  CurrentInsightResponse,
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
