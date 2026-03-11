import { api } from "@/lib/api";
import type { AxiosError } from "axios";

export interface HealthScoreCurrent {
  id: string;
  tenant_id: string;
  total_score: number;
  level: string;
  triggered_by: string;
  snapshot_date: string;
  created_at: string;
}

export const healthScoreService = {
  async getCurrent(): Promise<HealthScoreCurrent | null> {
    try {
      const res = await api.get<HealthScoreCurrent>("/health-scores/current");
      return res.data;
    } catch (err) {
      const axiosErr = err as AxiosError;
      if (axiosErr.response?.status === 404) return null;
      return null;
    }
  },
};
