import { api } from "@/lib/api";

export interface HealthConfig {
  target_margin_pct: number;
  warning_margin_pct: number;
  is_custom: boolean;
}

export const settingsService = {
  async getHealthConfig(): Promise<HealthConfig | null> {
    try {
      const res = await api.get<HealthConfig>("/settings/health-config");
      return res.data;
    } catch {
      return null;
    }
  },

  async updateHealthConfig(
    target_margin_pct: number,
    warning_margin_pct: number,
  ): Promise<HealthConfig | null> {
    try {
      const res = await api.patch<HealthConfig>("/settings/health-config", {
        target_margin_pct,
        warning_margin_pct,
      });
      return res.data;
    } catch {
      return null;
    }
  },

  async resetHealthConfig(): Promise<HealthConfig | null> {
    try {
      const res = await api.delete<HealthConfig>("/settings/health-config");
      return res.data;
    } catch {
      return null;
    }
  },
};
