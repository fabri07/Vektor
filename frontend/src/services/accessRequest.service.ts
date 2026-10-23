/**
 * Cliente HTTP del trámite público de solicitud de acceso.
 *
 * Contrato: `backend/app/api/v1/access_requests.py` (router público, sin auth).
 * El payload declara EXACTAMENTE los campos del schema del backend: `extra="forbid"`
 * convierte cualquier campo de más en un 422. Notablemente **no hay `password`** —
 * la cuenta se acuña recién cuando el dueño aprueba la solicitud.
 */

import { api } from "@/lib/api";
import type { GooglePrefillResponse } from "@/types/api";
import type {
  CanShareFiles,
  HistoryDepth,
  MainConcern,
  RecordsFormat,
  RequestedPlan,
  RevenueBand,
  StaffSize,
  YearsOperating,
} from "@/lib/accessRequestOptions";
import { CONSENT_NOTICE_VERSION } from "@/lib/privacyNotices";
import type { RequestedVertical } from "@/lib/verticals";
import type { AccessRequestInput } from "@/validation/accessRequest";

/** Cuerpo exacto de `POST /access-requests`. Ni un campo más. */
export interface AccessRequestPayload {
  full_name: string;
  email: string;
  phone: string | null;
  business_name: string;

  requested_vertical: RequestedVertical;
  vertical_other_text: string | null;

  requested_plan: RequestedPlan;

  years_operating: YearsOperating;
  staff_size: StaffSize;
  monthly_revenue_band: RevenueBand;
  main_concern: MainConcern;
  records_format: RecordsFormat;
  history_depth: HistoryDepth;
  can_share_files: CanShareFiles;
  records_notes: string | null;
  applicant_notes: string | null;

  /** Literal `true`: el backend lo tipa como `Literal[True]`. */
  consent: true;
  consent_version: string;

  cta_source: string | null;
  /** Token opaco del prefill de "Continuar con Google" (lo puebla el alta social). */
  google_prefill_token?: string;

  /** Honeypot: viaja siempre vacío desde un humano. */
  website: string;
  /** ms entre el montaje del formulario y el envío. */
  elapsed_ms: number;
}

export interface AccessRequestAcceptedResponse {
  status: string;
  message: string;
}

export interface VerifiedAccessRequestResponse {
  status: string;
  message: string;
  requested_plan: RequestedPlan;
}

/** Recorta un texto opcional; `""`/`"   "` colapsan a `null`, nunca a `""`. */
function trimOrNull(value: string | null | undefined): string | null {
  const limpio = (value ?? "").trim();
  return limpio.length > 0 ? limpio : null;
}

export interface AccessRequestMetadata {
  ctaSource: string | null;
  website: string;
  elapsedMs: number;
  googlePrefillToken?: string;
}

/**
 * Traduce el input validado al payload HTTP.
 *
 * `vertical_other_text` va `null` salvo que el rubro sea `otros`: mandarlo con
 * cualquier otro rubro es 422 del lado del backend.
 */
export function buildAccessRequestPayload(
  input: AccessRequestInput,
  meta: AccessRequestMetadata,
): AccessRequestPayload {
  return {
    full_name: input.full_name,
    email: input.email,
    phone: trimOrNull(input.phone),
    business_name: input.business_name,

    requested_vertical: input.requested_vertical,
    vertical_other_text:
      input.requested_vertical === "otros" ? trimOrNull(input.vertical_other_text) : null,

    requested_plan: input.requested_plan,

    years_operating: input.years_operating,
    staff_size: input.staff_size,
    monthly_revenue_band: input.monthly_revenue_band,
    main_concern: input.main_concern,
    records_format: input.records_format,
    history_depth: input.history_depth,
    can_share_files: input.can_share_files,
    records_notes: trimOrNull(input.records_notes),
    applicant_notes: trimOrNull(input.applicant_notes),

    consent: true,
    consent_version: CONSENT_NOTICE_VERSION,

    cta_source: meta.ctaSource,
    ...(meta.googlePrefillToken ? { google_prefill_token: meta.googlePrefillToken } : {}),

    website: meta.website,
    elapsed_ms: meta.elapsedMs,
  };
}

export async function createAccessRequest(
  payload: AccessRequestPayload,
): Promise<AccessRequestAcceptedResponse> {
  const res = await api.post<AccessRequestAcceptedResponse>("/access-requests", payload);
  return res.data;
}

/** Doble opt-in: POST y no GET, para que los escáneres de correo no lo consuman. */
export async function verifyAccessRequest(
  token: string,
): Promise<VerifiedAccessRequestResponse> {
  const res = await api.post<VerifiedAccessRequestResponse>("/access-requests/verify", {
    token,
  });
  return res.data;
}

/** Reenvío del mail de confirmación. Responde 200 genérico siempre. */
export async function resendAccessRequestVerification(email: string): Promise<void> {
  await api.post("/access-requests/resend", { email });
}

/**
 * Datos de la identidad de Google detrás de un token de prefill.
 *
 * **Es una lectura: NO consume el token.** El mismo token viaja después en el
 * POST de la solicitud, que es donde el backend resuelve el `google_subject` y
 * recién ahí lo consume. Un token vencido o ya canjeado devuelve 404 y el
 * formulario se completa a mano.
 */
export async function fetchGooglePrefill(
  token: string,
): Promise<GooglePrefillResponse> {
  const res = await api.get<GooglePrefillResponse>(
    `/access-requests/prefill/${encodeURIComponent(token)}`,
  );
  return res.data;
}
