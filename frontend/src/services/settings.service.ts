import { api } from "@/lib/api";
import type { FiscalCondition } from "@/lib/fiscalCondition";

export type { FiscalCondition } from "@/lib/fiscalCondition";

export interface HealthConfig {
  target_margin_pct: number;
  warning_margin_pct: number;
  is_custom: boolean;
}

export interface WorkSchedule {
  work_days: number[];
  work_open_hour: number;
  work_close_hour: number;
  is_default: boolean;
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

  async getWorkSchedule(): Promise<WorkSchedule | null> {
    try {
      const res = await api.get<WorkSchedule>("/settings/work-schedule");
      return res.data;
    } catch {
      return null;
    }
  },

  async updateWorkSchedule(payload: {
    work_days: number[];
    work_open_hour: number;
    work_close_hour: number;
  }): Promise<WorkSchedule> {
    const res = await api.patch<WorkSchedule>("/settings/work-schedule", payload);
    return res.data;
  },

  async getFiscalCondition(): Promise<FiscalCondition | null> {
    try {
      const res = await api.get<{ fiscal_condition: FiscalCondition | null }>(
        "/settings/fiscal-condition",
      );
      return res.data.fiscal_condition;
    } catch {
      return null;
    }
  },

  async updateFiscalCondition(value: FiscalCondition | null): Promise<void> {
    await api.patch("/settings/fiscal-condition", { fiscal_condition: value });
  },
};
