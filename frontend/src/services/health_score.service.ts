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

export interface HealthScoreLatest {
  id: string;
  tenant_id: string;
  score_total: number;
  score_cash: number;
  score_margin: number;
  score_stock: number;
  score_supplier: number;
  primary_risk_code: string;
  confidence_level: string;
  data_completeness_score: number;
  level: string;
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

  async getLatest(): Promise<HealthScoreLatest | null> {
    try {
      const res = await api.get<HealthScoreLatest | { status: string }>(
        "/health-scores/latest",
      );
      if ("status" in res.data && res.data.status === "CALCULATING") {
        return null;
      }
      return res.data as HealthScoreLatest;
    } catch {
      return null;
    }
  },
};
