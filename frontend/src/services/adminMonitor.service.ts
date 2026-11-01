import { api } from "@/lib/api";

export interface MonitorNegocios {
  total: number;
  active: number;
  trial: number;
  read_only: number;
  suspended: number;
}

export interface MonitorSuscripciones {
  trial: number;
  active: number;
  grace: number;
  read_only: number;
  /** Negocios reales sin suscripción viva: dato roto, se muestra. */
  sin_suscripcion: number;
  vencen_en_7_dias: number;
}

export interface MonitorActividad {
  hoy: number;
  ultimos_7_dias: number;
  ultimos_30_dias: number;
}

export interface MonitorIA {
  dias: number;
  tokens_total: number;
  cost_usd: number;
  /** > 0 ⇒ `cost_usd` subestima el real. */
  unpriced_tokens: number;
  /** `YYYY-MM-DD` del último turno con tokens, o `null` si nunca hubo. */
  ultimo_consumo: string | null;
}

export type MotivoDeAtencion = "GRACE" | "READ_ONLY" | "TRIAL_ENDING" | "INACTIVE";

export interface MonitorAtencion {
  tenant_id: string;
  display_name: string;
  motivo: MotivoDeAtencion;
  /** Fecha o fecha-hora ISO que explica el motivo; `null` si no hay. */
  fecha: string | null;
}

export interface MonitorResponse {
  generado_en: string;
  negocios: MonitorNegocios;
  suscripciones: MonitorSuscripciones;
  actividad: MonitorActividad;
  ia: MonitorIA;
  atencion: MonitorAtencion[];
  atencion_total: number;
}

export const adminMonitorService = {
  async get(): Promise<MonitorResponse> {
    const res = await api.get<MonitorResponse>("/admin/monitor");
    return res.data;
  },
};
