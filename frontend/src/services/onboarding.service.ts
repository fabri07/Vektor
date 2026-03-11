import { api } from "@/lib/api";

export interface OnboardingSubmitPayload {
  vertical_code: string;
  weekly_sales_estimate_ars: number;
  monthly_inventory_cost_ars: number;
  monthly_fixed_expenses_ars: number;
  cash_on_hand_ars: number;
  product_count_estimate: number;
  supplier_count_estimate: number;
  main_concern: "MARGIN" | "STOCK" | "CASH";
}

export interface OnboardingSubmitResult {
  snapshot_id: string;
  data_completeness_score: number;
  confidence_level: string;
  message: string;
}

export interface OnboardingStatus {
  completed: boolean;
  vertical_code: string;
  data_completeness_score: number | null;
}

export const onboardingService = {
  async getStatus(): Promise<OnboardingStatus> {
    const res = await api.get<OnboardingStatus>("/onboarding/status");
    return res.data;
  },

  async submit(payload: OnboardingSubmitPayload): Promise<OnboardingSubmitResult> {
    const res = await api.post<OnboardingSubmitResult>("/onboarding/submit", payload);
    return res.data;
  },
};
