import { api } from "@/lib/api";
import type { FiscalCondition } from "@/lib/fiscalCondition";

/**
 * Cuerpo de `POST /onboarding/submit`.
 *
 * **No lleva `vertical_code`.** El schema del backend
 * (`OnboardingSubmitRequest`) lo sacó cuando el vertical pasó a asignarse al
 * aprobar la solicitud, y usa `extra="forbid"`: mandarlo es un 422 que deja al
 * tenant sin poder completar el onboarding ni obtener su score.
 */
export interface OnboardingSubmitPayload {
  weekly_sales_estimate_ars: number;
  /*
   * Los tres montos aceptan `null` = "no contestó", que NO es cero.
   *
   * Antes eran `number` y el formulario mandaba `parseFloat(campo) || 0`: un
   * campo en blanco entraba como un cero afirmado, el backend lo persistía como
   * estimación del dueño y el score lo usaba para calcular. `null` viaja como
   * ausencia y el backend baja la confianza en vez de inventar el número.
   */
  monthly_inventory_cost_ars: number | null;
  monthly_fixed_expenses_ars: number | null;
  cash_on_hand_ars: number | null;
  product_count_estimate: number;
  supplier_count_estimate: number;
  /**
   * Opcional: si el visitante ya la declaró al pedir acceso, se omite y el
   * backend la toma de `business_profiles.custom_fields` (la selló la
   * aprobación). Solo viaja cuando el onboarding tuvo que preguntarla.
   */
  main_concern?: "MARGIN" | "STOCK" | "CASH";
  work_days?: number[];
  work_open_hour?: number;
  work_close_hour?: number;
  /** Régimen fiscal — opcional y salteable en el onboarding. */
  fiscal_condition?: FiscalCondition;
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
  /**
   * Preocupación principal ya declarada al pedir acceso. `null` = no hay
   * valor confiable y el onboarding tiene que preguntarla.
   */
  main_concern: "MARGIN" | "STOCK" | "CASH" | null;
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
