import { api } from "@/lib/api";

/** Estado de CUENTA (lo decide el soporte). No es el de la suscripción. */
export type EstadoDeCuenta = "ACTIVE" | "TRIAL" | "READ_ONLY" | "SUSPENDED";

export interface AdminTenantListItem {
  tenant_id: string;
  display_name: string;
  legal_name: string;
  vertical_code: string | null;
  account_status: string;
  status_changed_at: string | null;
  is_demo: boolean;
  created_at: string;
  /** Estado de SUSCRIPCIÓN, independiente del de cuenta. `null` = sin suscripción viva. */
  subscription_plan: string | null;
  subscription_status: string | null;
  current_period_end: string | null;
  users_count: number;
  /** Último login conocido: NO es actividad real. */
  last_login_at: string | null;
  tokens_30d: number;
}

export interface AdminTenantListResponse {
  items: AdminTenantListItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface AdminTenantSubscription {
  plan_code: string;
  status: string;
  current_period_start: string | null;
  current_period_end: string | null;
  trial_ends_at: string | null;
  grace_ends_at: string | null;
  cancel_at_period_end: boolean;
  seats_included: number;
  granted_ia_queries_per_month: number | null;
  granted_imports_per_month: number | null;
  granted_photo_pdf_reads_per_month: number | null;
}

export interface AdminTenantUser {
  user_id: string;
  email: string;
  full_name: string;
  role_code: string;
  is_active: boolean;
  last_login_at: string | null;
}

export interface AdminTenantAuditEvent {
  decision_type: string;
  created_at: string;
  actor_user_id: string | null;
  decision_data: Record<string, unknown>;
}

export interface AdminTenantDetail {
  tenant_id: string;
  display_name: string;
  legal_name: string;
  vertical_code: string | null;
  account_status: string;
  status_changed_at: string | null;
  is_demo: boolean;
  created_at: string;
  subscription: AdminTenantSubscription | null;
  users: AdminTenantUser[];
  recent_events: AdminTenantAuditEvent[];
}

export interface TenantStatusChangeResponse {
  tenant_id: string;
  previous_status: string;
  status: string;
  /** `false` = ya estaba en ese estado: no se tocó nada. */
  changed: boolean;
  status_changed_at: string | null;
}

export interface SubscriptionPaymentInput {
  expected: "activate" | "renew";
  reference: string;
  amount_usd: string;
  amount_ars: string;
  plan_code?: string | null;
  notes?: string | null;
}

export interface SubscriptionPaymentResponse {
  kind: string;
  plan_code: string;
  period_start: string;
  period_end: string;
  already_applied: boolean;
}

export interface SubscriptionPaymentItem {
  id: string;
  source: string;
  reference: string;
  kind: string;
  plan_code: string;
  amount_usd: string;
  amount_ars: string;
  operator: string;
  period_start: string;
  period_end: string;
  notes: string | null;
  created_at: string;
}

export interface ListTenantsParams {
  page?: number;
  page_size?: number;
  q?: string;
  account_status?: string;
}

const base = (id: string) => `/admin/tenants/${id}`;

export const adminTenantsService = {
  async list(params: ListTenantsParams = {}): Promise<AdminTenantListResponse> {
    const { data } = await api.get<AdminTenantListResponse>("/admin/tenants", { params });
    return data;
  },
  async get(id: string): Promise<AdminTenantDetail> {
    const { data } = await api.get<AdminTenantDetail>(base(id));
    return data;
  },
  async changeStatus(
    id: string,
    body: { status: EstadoDeCuenta; reason: string },
  ): Promise<TenantStatusChangeResponse> {
    const { data } = await api.post<TenantStatusChangeResponse>(`${base(id)}/status`, body);
    return data;
  },
  async applyPayment(
    id: string,
    body: SubscriptionPaymentInput,
  ): Promise<SubscriptionPaymentResponse> {
    const { data } = await api.post<SubscriptionPaymentResponse>(
      `${base(id)}/subscription/payments`,
      body,
    );
    return data;
  },
  async changePlan(id: string, planCode: string): Promise<{ scheduled_plan_code: string }> {
    const { data } = await api.post<{ scheduled_plan_code: string }>(
      `${base(id)}/subscription/change-plan`,
      { plan_code: planCode },
    );
    return data;
  },
  async cancelRenewal(id: string, reason: string | null): Promise<{ access_until: string }> {
    const { data } = await api.post<{ access_until: string }>(
      `${base(id)}/subscription/cancel`,
      { reason },
    );
    return data;
  },
  async listPayments(id: string): Promise<SubscriptionPaymentItem[]> {
    const { data } = await api.get<{ items: SubscriptionPaymentItem[] }>(
      `${base(id)}/subscription/payments`,
    );
    return data.items;
  },
};

/** Texto del 409/422 estructurado del servidor, o un mensaje genérico. */
export function adminErrorMessage(error: unknown): string {
  const detalle = (
    error as { response?: { data?: { detail?: unknown } } } | undefined
  )?.response?.data?.detail;
  if (detalle && typeof detalle === "object" && !Array.isArray(detalle)) {
    const mensaje = (detalle as { message?: string }).message;
    if (mensaje) return mensaje;
  }
  if (typeof detalle === "string") return detalle;
  return "No se pudo completar la acción. Intentá de nuevo.";
}
