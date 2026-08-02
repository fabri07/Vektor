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
  score_growth: number | null;   // null = snapshot v1 (pre-Stage-5a)
  primary_risk_code: string;
  confidence_level: string;
  data_completeness_score: number;
  level: string;
  created_at: string;
}

/** ¿El payload es uno de los estados sin score (trae `status`)? */
function esPayloadDeEstado(data: unknown): data is { status: string } {
  return typeof data === "object" && data !== null && "status" in data;
}

/**
 * Clasifica cualquier respuesta de `/health-scores/latest`, que tiene TRES
 * formas (backend `app/api/v1/health_scores.py`): el score,
 * `{status: "CALCULATING"}` y `{status: "NO_DATA", score: null, …}`.
 *
 * Es la ÚNICA definición de "esto es un score / esto no lo es". El dashboard
 * tenía su propia copia que sólo reconocía `CALCULATING`, y por eso un
 * `NO_DATA` le pasaba de largo hasta el cast `as HealthScoreV2Response` y se
 * renderizaba alrededor de un objeto sin `score_total`.
 */
export function clasificarLatestScore(
  data: unknown,
): "score" | "calculating" | "no_data" {
  if (data == null) return "calculating";
  if (!esPayloadDeEstado(data)) return "score";
  return data.status === "NO_DATA" ? "no_data" : "calculating";
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

  async getHistoryV2(): Promise<HealthScoreLatest[]> {
    try {
      const res = await api.get<HealthScoreLatest[]>("/health-scores/history/v2");
      return res.data;
    } catch {
      return [];
    }
  },

  async exportReport(
    snapshotId: string,
    format: "pdf" | "docx",
    narrative = "",
  ): Promise<Blob | null> {
    try {
      const res = await api.post(
        `/health-scores/${snapshotId}/export`,
        { format, narrative },
        { responseType: "blob" },
      );
      return res.data as Blob;
    } catch {
      return null;
    }
  },
};
