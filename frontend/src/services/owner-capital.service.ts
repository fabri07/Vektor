import { api } from "@/lib/api";

/** Capital de los socios (D·2/D·4): `/capital-movements`. Sólo OWNER y ADMIN. */

export type CapitalKind = "aporte" | "retiro" | "prestamo_socio" | "devolucion_prestamo";

export interface CapitalMovement {
  id: string;
  kind: CapitalKind;
  form: "dinero" | "especie";
  amount_ars: number;
  occurred_on: string; // YYYY-MM-DD
  payment_method: string | null;
  partner_name: string | null;
  notes: string | null;
  source_type: string;
  created_at: string;
  created_by_user_id: string | null;
  voided_at: string | null;
  void_reason: string | null;
}

export interface CapitalAmountsByForm {
  dinero: number;
  especie: number;
}

export interface CapitalPartnerSummary {
  /** null = movimientos sin socio identificado. */
  partner_name: string | null;
  aportes: CapitalAmountsByForm;
  retiros: CapitalAmountsByForm;
  prestamos_recibidos: CapitalAmountsByForm;
  devoluciones: CapitalAmountsByForm;
  /** Acumulado a `to_date`. Puede ser negativo (inconsistencia de carga). */
  prestamo_pendiente: number;
  prestamo_pendiente_inconsistente: boolean;
}

export interface CapitalSummaryResponse {
  from_date: string;
  to_date: string;
  has_data: boolean;
  has_in_kind: boolean;
  partners: CapitalPartnerSummary[];
  totals: CapitalPartnerSummary | null;
  /** Flujo en dinero; no incluye el capital inicial declarado en el alta. */
  cash_flow: { entradas: number; salidas: number; neto: number } | null;
  /**
   * Capital inicial declarado en el onboarding (D·5) que cae en el período. Ya
   * está sumado en los aportes; viene aparte para rotularlo. `null` si no hay.
   */
  initial_capital_declared?: { amount_ars: number; occurred_on: string } | null;
}

export interface CreateCapitalMovementPayload {
  kind: CapitalKind;
  amount: number;
  occurred_on: string; // YYYY-MM-DD
  payment_method: string;
  partner_name?: string | null;
  notes?: string | null;
}

export const CAPITAL_LIST_LIMIT = 500;

export const ownerCapitalService = {
  async getSummary(params: { from_date: string; to_date: string }): Promise<CapitalSummaryResponse> {
    const res = await api.get<CapitalSummaryResponse>("/capital-movements/summary", { params });
    return res.data;
  },

  // Tope del backend (`limit` ≤ 500). El listado viene del más nuevo al más
  // viejo: si llegan exactamente estos, puede haber más y la pantalla lo avisa.
  async list(params: { from_date: string; to_date: string }): Promise<CapitalMovement[]> {
    const res = await api.get<{ items: CapitalMovement[] }>("/capital-movements", {
      params: { ...params, limit: CAPITAL_LIST_LIMIT },
    });
    return res.data.items;
  },

  async create(
    payload: CreateCapitalMovementPayload,
    idempotencyKey: string,
  ): Promise<CapitalMovement> {
    const res = await api.post<CapitalMovement>("/capital-movements", payload, {
      headers: { "Idempotency-Key": idempotencyKey },
    });
    return res.data;
  },

  async void(id: string, reason: string): Promise<CapitalMovement> {
    const res = await api.post<CapitalMovement>(`/capital-movements/${id}/void`, { reason });
    return res.data;
  },
};
